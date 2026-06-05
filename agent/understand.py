"""Stage 2 — Understand.

Build a ``RepoContext`` from a local clone: a filtered file tree, cheap per-file
skeletons (package/import/func/type lines), the top-N files most relevant to the
issue by word overlap, and the full contents of those relevant files.

For go-playground/validator, ``baked_in.go`` and ``baked_in_test.go`` are always
included regardless of score — almost every issue touches them.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

from .ingest import IssueContext
from .settings import Settings, load_settings

_MAX_FILES = 300
_TOP_N_RELEVANT = 5
_FULL_CONTENT_LINE_CAP = 600
_SKELETON_GREP = r"^func \|^type \|^package \|^import "

# Always-relevant files for go-playground/validator. Current master keeps all
# built-in validators in baked_in.go and their tests in validator_test.go
# (the historical baked_in_test.go was folded into validator_test.go).
_ALWAYS_RELEVANT = ["baked_in.go", "validator_test.go"]

_WORD_RE = re.compile(r"[a-z0-9]+")
# Matches a Go filename mentioned in prose, e.g. "regexes.go" or "internal/foo.go".
_GOFILE_RE = re.compile(r"[\w./-]*?\b(\w+\.go)\b")


@dataclass
class RepoContext:
    file_tree: str = ""                              # filtered directory listing
    skeletons: dict[str, str] = field(default_factory=dict)   # path -> signatures/types
    relevant_files: list[str] = field(default_factory=list)   # top files for the issue
    full_contents: dict[str, str] = field(default_factory=dict)  # path -> full content


def _collect_go_files(repo_path: str, settings: Settings) -> list[str]:
    """Walk the repo, return repo-relative .go paths, skipping excluded dirs."""
    excluded = set(settings.excluded_dirs)
    extensions = tuple(settings.repo_file_extensions)
    found: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in files:
            if name.endswith(extensions):
                rel = os.path.relpath(os.path.join(root, name), repo_path)
                found.append(rel)
                if len(found) >= _MAX_FILES:
                    return sorted(found)
    return sorted(found)


def _build_tree(rel_paths: list[str]) -> str:
    """Render relative paths as an indented tree string."""
    lines: list[str] = []
    seen_dirs: set[str] = set()
    for path in sorted(rel_paths):
        parts = path.split(os.sep)
        for depth in range(len(parts) - 1):
            dir_key = os.sep.join(parts[: depth + 1])
            if dir_key not in seen_dirs:
                seen_dirs.add(dir_key)
                lines.append("  " * depth + parts[depth] + "/")
        lines.append("  " * (len(parts) - 1) + parts[-1])
    return "\n".join(lines)


def _skeleton(repo_path: str, rel_path: str) -> str:
    """Extract package/import/func/type lines for one file via grep."""
    full = os.path.join(repo_path, rel_path)
    try:
        result = subprocess.run(
            ["grep", "-n", "-E", _SKELETON_GREP.replace(r"\|", "|"), full],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    out = result.stdout.strip()
    if not out:
        return ""
    return "\n".join(f"{rel_path}: line {line}" for line in out.splitlines())


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _files_named_in_issue(issue: IssueContext, go_files: list[str]) -> list[str]:
    """Return repo files whose basename is explicitly mentioned in the issue text.

    Issues frequently name the fix site directly ("cronRegexString in regexes.go").
    Skeleton-based scoring is blind to data files (regex/code tables have almost no
    func/type lines), so we surface any named file that actually exists in the repo.
    """
    mentioned = {m.lower() for m in _GOFILE_RE.findall(f"{issue.title} {issue.body}")}
    if not mentioned:
        return []
    by_basename = {os.path.basename(rel).lower(): rel for rel in go_files}
    return [by_basename[name] for name in mentioned if name in by_basename]


def _read_capped(repo_path: str, rel_path: str, line_cap: int) -> str:
    full = os.path.join(repo_path, rel_path)
    if not os.path.isfile(full):
        return ""
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if len(lines) > line_cap:
        return "".join(lines[:line_cap]) + f"\n... [truncated to first {line_cap} lines]"
    return "".join(lines)


def understand(repo_path: str, issue: IssueContext, settings: Settings | None = None) -> RepoContext:
    settings = settings or load_settings()
    repo_path = os.path.realpath(repo_path)
    if not os.path.isdir(repo_path):
        raise FileNotFoundError(f"repo path does not exist: {repo_path}")

    # Step A — file tree.
    go_files = _collect_go_files(repo_path, settings)
    file_tree = _build_tree(go_files)

    # Step B — skeletons.
    skeletons: dict[str, str] = {}
    for rel in go_files:
        sk = _skeleton(repo_path, rel)
        if sk:
            skeletons[rel] = sk

    # Step C — relevance scoring by word overlap of issue text vs. skeleton.
    issue_words = _tokenize(f"{issue.title} {issue.body}")
    scored: list[tuple[int, str]] = []
    for rel, sk in skeletons.items():
        score = len(issue_words & _tokenize(sk))
        scored.append((score, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    relevant = [rel for _, rel in scored[:_TOP_N_RELEVANT]]

    # Force-include any files the issue names explicitly (e.g. "regexes.go").
    for named in _files_named_in_issue(issue, go_files):
        if named not in relevant:
            relevant.append(named)

    # Always include the validator hot files if present in the repo.
    for forced in _ALWAYS_RELEVANT:
        if forced not in relevant and os.path.isfile(os.path.join(repo_path, forced)):
            relevant.append(forced)

    # Step D — full contents of relevant files.
    full_contents: dict[str, str] = {}
    for rel in relevant:
        content = _read_capped(repo_path, rel, _FULL_CONTENT_LINE_CAP)
        if content:
            full_contents[rel] = content

    return RepoContext(
        file_tree=file_tree,
        skeletons=skeletons,
        relevant_files=relevant,
        full_contents=full_contents,
    )
