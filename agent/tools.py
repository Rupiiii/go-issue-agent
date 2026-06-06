"""Tool definitions and executors for Stage 4 (implement).

Four tools are exposed to the model: ``read_file``, ``search_code``, ``write_file``,
and ``run_go_command``. ``TOOL_DEFINITIONS`` is the JSON-schema list passed to the
Anthropic API; ``ToolExecutor`` runs the calls locally against a single repo root and
tracks every file the model writes (consumed by Stages 5 and 6).

All filesystem access is confined to the repo root via a path-escape check.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from typing import Any

from .llm import ToolCall

# Whitelisted Go commands. Anything else is rejected.
_ALLOWED_GO_COMMANDS = {
    "go build ./...",
    "go test ./...",
    "go vet ./...",
    "go fmt ./...",
}

_READ_FILE_LINE_CAP = 8000      # max lines for a full-file read
_READ_RANGE_LINE_CAP = 2000     # max lines for a ranged read window
_SEARCH_OUTPUT_LINE_CAP = 200
_DEFAULT_GO_TIMEOUT = 120
_SEARCH_EXCLUDED_DIRS = {"vendor", ".git", "node_modules", "testdata"}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a file in the repository. Returns the whole file if small. For large "
            "files, pass start_line/end_line to read a specific region (use search_code "
            "first to find the line number you need). Output lines are numbered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from repo root, e.g. baked_in.go",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-based first line to read. Use with end_line for large files.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-based last line to read (inclusive).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a pattern across all Go files in the repository using ripgrep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search pattern (literal string or regex)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional glob, e.g. '*.go'",
                    "default": "*.go",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a targeted edit by replacing an exact, unique snippet. PREFERRED for "
            "changing existing files — especially large ones — because you only supply the "
            "snippet, not the whole file. old_string must appear EXACTLY once (include a "
            "few surrounding lines to make it unique) and be reproduced byte-for-byte "
            "(preserve all backslashes/escapes). To insert a new test case, set old_string "
            "to an existing nearby block and new_string to that block plus your addition."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
                "old_string": {
                    "type": "string",
                    "description": "Exact existing text to replace; must be unique in the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a file, replacing it entirely. Use ONLY for creating a new file or "
            "replacing a small one. For edits to existing/large files, use edit_file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
                "content": {
                    "type": "string",
                    "description": "Full file content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_go_command",
        "description": "Run a Go toolchain command in the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_GO_COMMANDS),
                    "description": "The exact Go command to run.",
                }
            },
            "required": ["command"],
        },
    },
]


def _enclosing_func_bounds(lines: list[str], target_idx: int) -> tuple[int, int] | None:
    """Return (start_idx, end_idx) — 0-based inclusive — of the top-level Go function that
    encloses ``target_idx``, or None if the target is not inside one.

    Relies on gofmt layout: a top-level ``func …{`` begins at column 0 and its matching
    ``}`` is the next line that begins at column 0 (nested braces are indented). This is
    robust for gofmt'd Go (the target repos all are) and needs no Go parser.
    """
    if target_idx < 0 or target_idx >= len(lines):
        return None
    func_start = None
    for i in range(target_idx, -1, -1):
        if lines[i].startswith("func "):
            func_start = i
            break
        # A column-0 '}' reached while scanning back (before any func) means the target
        # sits between/after functions, not inside a function body.
        if i < target_idx and lines[i].startswith("}"):
            return None
    if func_start is None:
        return None
    for j in range(func_start + 1, len(lines)):
        if lines[j].startswith("}"):
            return (func_start, j) if j >= target_idx else None
    return None


class ToolExecutor:
    def __init__(self, repo_root: str, go_timeout: int = _DEFAULT_GO_TIMEOUT):
        self.repo_root = os.path.realpath(repo_root)
        self.go_timeout = go_timeout
        self.written_files: list[str] = []

    # ----- path safety -----------------------------------------------------

    def _resolve(self, rel_path: str) -> str:
        """Resolve a repo-relative path and ensure it stays inside the repo root."""
        candidate = os.path.realpath(os.path.join(self.repo_root, rel_path))
        if candidate != self.repo_root and not candidate.startswith(self.repo_root + os.sep):
            raise ValueError(f"path escapes repository root: {rel_path}")
        return candidate

    # ----- individual tools ------------------------------------------------

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        try:
            full = self._resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not os.path.isfile(full):
            return f"ERROR: file not found: {path}"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)

        # Ranged read: return the requested 1-based inclusive window (bounded). If the
        # start falls inside a Go function, expand the window to the WHOLE enclosing
        # function — the model tends to pick too-narrow windows and then edit without
        # seeing the rest of the function's control flow (the cause of mis-placed edits).
        if start_line is not None or end_line is not None:
            req_start = max(1, start_line or 1)
            if req_start > total:
                return f"ERROR: start_line {req_start} is past end of file ({total} lines)"
            bounds = _enclosing_func_bounds(lines, req_start - 1)
            expanded = False
            if bounds is not None:
                fstart, fend = bounds  # 0-based inclusive
                start = min(req_start, fstart + 1)
                end = max(end_line or 0, fend + 1)
                expanded = (start, end) != (req_start, end_line or (fend + 1))
            else:
                start = req_start
                end = min(total, end_line or total)
            end = min(end, start + _READ_RANGE_LINE_CAP - 1, total)
            window = lines[start - 1 : end]
            note = " — expanded to enclosing function" if expanded else ""
            header = f"[lines {start}-{end} of {total} in {path}{note}]\n"
            return header + "".join(window)

        # Full read, capped.
        if total > _READ_FILE_LINE_CAP:
            body = "".join(lines[:_READ_FILE_LINE_CAP])
            return (
                body
                + f"\n... [showing first {_READ_FILE_LINE_CAP} of {total} lines. "
                "Use search_code to find a line number, then read_file with "
                "start_line/end_line to read further.]"
            )
        return "".join(lines)

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        try:
            full = self._resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not os.path.isfile(full):
            return f"ERROR: file not found: {path} (use write_file to create a new file)"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_string)
        if count == 0:
            return (
                "ERROR: old_string not found. It must match the file byte-for-byte. "
                "Read the exact text (read_file) and try again."
            )
        if count > 1:
            return (
                f"ERROR: old_string is not unique ({count} matches). Include more "
                "surrounding lines so it identifies exactly one location."
            )
        updated = content.replace(old_string, new_string, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(updated)
        if path not in self.written_files:
            self.written_files.append(path)
        delta = len(new_string.splitlines()) - len(old_string.splitlines())
        return f"OK: edited {path} (net {delta:+d} lines)"

    def search_code(self, query: str, file_pattern: str = "*.go") -> str:
        cmd = [
            "rg",
            "--line-number",
            "--no-heading",
            query,
            "--glob",
            file_pattern,
            self.repo_root,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            # ripgrep not installed — fall back to a pure-Python search so the agent is
            # never left without code search.
            return self._python_search(query, file_pattern)
        except subprocess.TimeoutExpired:
            return "ERROR: search timed out."

        out = result.stdout
        if not out.strip():
            # rg exits 1 with no matches; surface stderr only if it's a real error.
            if result.returncode not in (0, 1) and result.stderr.strip():
                return f"ERROR: {result.stderr.strip()}"
            return "(no matches)"
        return self._cap_search_output(out.splitlines())

    def _python_search(self, query: str, file_pattern: str = "*.go") -> str:
        """ripgrep-free fallback: walk the repo and grep matching files in Python."""
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = None  # treat query as a literal substring

        matches: list[str] = []
        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in _SEARCH_EXCLUDED_DIRS]
            for name in files:
                if not fnmatch.fnmatch(name, file_pattern):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.repo_root)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            hit = pattern.search(line) if pattern else (query in line)
                            if hit:
                                matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                                if len(matches) > _SEARCH_OUTPUT_LINE_CAP:
                                    break
                except OSError:
                    continue
            if len(matches) > _SEARCH_OUTPUT_LINE_CAP:
                break
        if not matches:
            return "(no matches)"
        return self._cap_search_output(matches)

    @staticmethod
    def _cap_search_output(lines: list[str]) -> str:
        if len(lines) > _SEARCH_OUTPUT_LINE_CAP:
            lines = lines[:_SEARCH_OUTPUT_LINE_CAP]
            lines.append(f"... [truncated to first {_SEARCH_OUTPUT_LINE_CAP} matches]")
        return "\n".join(lines)

    def write_file(self, path: str, content: str) -> str:
        try:
            full = self._resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        os.makedirs(os.path.dirname(full) or self.repo_root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        if path not in self.written_files:
            self.written_files.append(path)
        return f"OK: wrote {len(content)} bytes to {path}"

    def run_go_command(self, command: str) -> str:
        if command not in _ALLOWED_GO_COMMANDS:
            return (
                f"ERROR: command not allowed: {command!r}. "
                f"Allowed: {sorted(_ALLOWED_GO_COMMANDS)}"
            )
        try:
            result = subprocess.run(
                command.split(),
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.go_timeout,
            )
        except FileNotFoundError:
            return "ERROR: the 'go' toolchain is not installed or not on PATH."
        except subprocess.TimeoutExpired:
            return f"ERROR: '{command}' timed out after {self.go_timeout}s."
        output = (result.stdout or "") + (result.stderr or "")
        status = "exit 0" if result.returncode == 0 else f"exit {result.returncode}"
        return f"[{command} -> {status}]\n{output}" if output.strip() else f"[{command} -> {status}] (no output)"

    # ----- dispatch --------------------------------------------------------

    def execute(self, tool_call: ToolCall) -> str:
        name = tool_call.name
        args = tool_call.input or {}
        try:
            if name == "read_file":
                return self.read_file(
                    args["path"], args.get("start_line"), args.get("end_line")
                )
            if name == "search_code":
                return self.search_code(args["query"], args.get("file_pattern", "*.go"))
            if name == "edit_file":
                return self.edit_file(args["path"], args["old_string"], args["new_string"])
            if name == "write_file":
                return self.write_file(args["path"], args["content"])
            if name == "run_go_command":
                return self.run_go_command(args["command"])
        except KeyError as exc:
            return f"ERROR: missing required argument {exc} for tool {name}"
        except Exception as exc:  # never let a tool crash the loop
            return f"ERROR: tool {name} failed: {exc}"
        return f"ERROR: unknown tool: {name}"


def execute_tool(tool_call: ToolCall, executor: ToolExecutor) -> str:
    """Convenience wrapper matching the CLAUDE.md signature shape."""
    return executor.execute(tool_call)
