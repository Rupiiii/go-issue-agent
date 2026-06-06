"""Stage 4 — Implement.

Drive a tool-calling loop: the model reads files, searches, writes full file
contents, and runs Go commands until it emits ``DONE`` or hits the iteration cap.
Before each LLM call, the conversation is measured against ``context_budget_tokens``
and the oldest tool *results* are truncated (never the calls, the initial message,
or the plan) to stay under budget.

A reusable ``retry_implementation`` entry point lets Stage 5 re-run the loop with
build-error context against the same ``ToolExecutor`` (so written files keep
accumulating).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .ingest import IssueContext
from .llm import CompletionResult, LLMClient
from .plan import FixPlan
from .settings import Settings
from .tools import TOOL_DEFINITIONS, ToolExecutor
from .understand import RepoContext

_TRUNCATION_NOTE = "[result truncated to fit context]"
_CHARS_PER_TOKEN = 4  # rough estimate for budget bookkeeping
_MAX_EMPTY_NUDGES = 2  # how many times to nudge a degenerate (empty) response before giving up

_NUDGE_MESSAGE = (
    "You ended your turn without completing the fix — no files have been changed yet. You "
    "are NOT done. Implement the plan now: use search_code/read_file to find the exact code, "
    "make each change with edit_file (use write_file only for new files), then call "
    'run_go_command with "go fmt ./..." and "go build ./...". Output DONE on its own line '
    "only after the fix is in place and the build succeeds."
)


@dataclass
class ImplementResult:
    modified_files: list[str] = field(default_factory=list)
    iterations_used: int = 0
    tool_call_log: list[dict] = field(default_factory=list)
    success: bool = False
    # Carried for Stage 5 retry; not part of the documented schema.
    executor: ToolExecutor | None = None
    messages: list[dict] = field(default_factory=list)
    project: str = ""  # repo_full_name, for the (repo-agnostic) retry system prompt


_SYSTEM_PROMPT_TEMPLATE = """\
You are a senior Go engineer fixing a GitHub issue in the {project} open-source project.

Tools: read_file, search_code, edit_file, write_file, run_go_command.

WORKFLOW
1. Use search_code to locate the symbol, test, or similar existing code you need — never
   guess a path, function, or name. Test files can be very large; search_code returns line
   numbers.
2. Read the exact text before changing it. For a large file, read the region with
   read_file start_line/end_line around the line search_code reported; the read auto-expands
   to the WHOLE enclosing function, so you see its full control flow before editing. Place
   your edit with the entire function in mind — a guard/return added mid-function must not
   skip code that runs below it.
3. Make the change with edit_file, then verify with the Go toolchain before finishing.

EDITING RULES — violating these breaks the build
- PREFER edit_file for changes to existing files: you supply only the exact snippet to
  replace (old_string, unique) and its replacement (new_string). This is the only way to
  edit a large file, and it avoids corrupting unrelated lines. To add a test case, set
  old_string to an existing nearby row/block and new_string to that block plus your row.
- Use write_file ONLY to create a new file or replace a small one; it rewrites the WHOLE
  file, so never use it on a large file.
- For edit_file, old_string MUST be copied verbatim from text you have JUST read with
  read_file — never anchor on remembered or guessed code. To insert into a long table or
  add a test case, first read the exact region (search_code for the line, then read_file
  around it), then reuse a real adjacent block (enough lines to be unique) as old_string.
- In BOTH tools, reproduce existing text byte-for-byte. Preserve backslashes and escapes
  EXACTLY as they appear on disk: double-quoted Go regex strings contain DOUBLED
  backslashes (the file literally has two backslashes before p, d, s, or a dot) — keep the
  same count. Leave raw strings (backticks) unchanged. Never unescape, re-escape, "tidy",
  or reflow text you are not deliberately changing.
- ADDITIVE-ONLY DISCIPLINE (the most important rule — over-editing is the #1 cause of
  regressions here). The ONLY lines that may disappear in an edit are lines that are
  THEMSELVES the bug. Everything else — every existing case, branch, return, and helper —
  must survive byte-for-byte. Do not reorder, "clean up", consolidate, or rewrite a
  function/switch to accommodate your change. If your edit would remove or restructure
  code that still needs to work, it is WRONG — make a smaller, insertion-style edit instead.
  Before each edit_file, check: "does old_string contain any line that is not the bug?" If
  yes, shrink it.
  Example — to stop a function panicking on a new input, ADD a guard at the top of the
  function; do NOT rewrite or delete the existing branches below it — they must keep working.
- Make the MINIMAL change that fixes the issue: fewest files, fewest lines. Prefer fixing
  logic in the relevant function over rewriting large data tables.
- Match the surrounding code style and the project's conventions exactly.
- Do not modify core, shared, or unrelated files unless the issue explicitly requires it.
- Do NOT add new third-party dependencies or edit go.mod / go.sum. The toolchain cannot
  fetch modules (there is no `go get`/`go mod tidy`), so a new import will fail to build.
  Solve the issue with the standard library and the dependencies already in the repo
  (e.g. fix the regex or the validator function itself).

TESTS
- If your fix changes behavior, add a case to the relevant existing table-driven test
  (the one named in the plan) that would FAIL before your change and PASS after. Match the
  existing row format exactly.
- BUG FIX RULE: If the issue body contains a concrete reproduction case (a code sample, a
  specific input, or a panic trace), you MUST write a test that exercises that EXACT input.
  Do not substitute a similar-but-different input. The test must fail (or panic) before your
  fix and pass after it. If your test would pass before you change anything, your test is
  wrong — rewrite it to actually reproduce the reported behavior.

FINISHING — output DONE on its own line ONLY when ALL of these hold
1. Every file in the plan is updated with full, correct content.
2. run_go_command "go fmt ./..." has been run.
3. run_go_command "go build ./..." returns exit 0 with no errors.
If a tool reports an error, read the offending file and fix it — do not stop with errors
outstanding, and do not re-read unchanged files. You have {max_iterations} iterations;
spend them finishing the job."""


def _format_plan(plan: FixPlan) -> str:
    return (
        f"Reasoning: {plan.reasoning}\n"
        f"Estimated complexity: {plan.estimated_complexity}\n"
        f"Files to modify: {plan.files_to_modify}\n"
        f"Files to create: {plan.files_to_create}\n"
        "Change descriptions:\n"
        + "\n".join(f"  - {d}" for d in plan.change_descriptions)
        + "\nConventions to follow:\n"
        + "\n".join(f"  - {c}" for c in plan.conventions_to_follow)
    )


def _build_initial_message(
    issue: IssueContext, plan: FixPlan, repo_ctx: RepoContext, conventions: str
) -> str:
    start_files = "\n\n".join(
        f"### {path}\n```go\n{repo_ctx.full_contents.get(path, '(not preloaded — read it with read_file)')}\n```"
        for path in plan.files_to_modify
    )
    return f"""## Issue
{issue.title}
{issue.body}

## Implementation plan
{_format_plan(plan)}

## Files to start with (already read for you)
{start_files}

## Conventions
{conventions}

Begin. Use search_code to locate what you need, read the exact region, then change it
with edit_file (minimal, escapes preserved byte-for-byte). Add the test case from the plan
with edit_file too. Then run "go fmt ./..." and "go build ./...". Output DONE only after
the build succeeds."""


def _estimate_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                total_chars += len(json.dumps(block, default=str))
    return total_chars // _CHARS_PER_TOKEN


def _truncate_to_budget(messages: list[dict], budget_tokens: int) -> None:
    """Truncate oldest tool_result contents in place until under budget.

    Index 0 (initial message) is never touched. Tool *calls* are preserved; only
    the result payloads shrink to a short note.
    """
    if _estimate_tokens(messages) <= budget_tokens:
        return
    for msg in messages[1:]:
        if _estimate_tokens(messages) <= budget_tokens:
            break
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_result" and block.get("content") != _TRUNCATION_NOTE:
                block["content"] = _TRUNCATION_NOTE


def _run_loop(
    llm: LLMClient,
    system_prompt: str,
    messages: list[dict],
    executor: ToolExecutor,
    max_iterations: int,
    settings: Settings,
    logger=None,
    tool_call_log: list[dict] | None = None,
) -> tuple[int, list[dict]]:
    """Run the tool-calling loop. Returns (iterations_used, tool_call_log)."""
    if tool_call_log is None:
        tool_call_log = []

    iterations_used = 0
    empty_nudges = 0
    for iteration in range(1, max_iterations + 1):
        iterations_used = iteration
        _truncate_to_budget(messages, settings.context_budget_tokens)

        result: CompletionResult = llm.complete(
            system=system_prompt, messages=messages, tools=TOOL_DEFINITIONS
        )
        if logger:
            logger.add_tokens(result.input_tokens, result.output_tokens)

        iter_calls: list[dict] = []

        if result.stop_reason != "tool_use":
            # No tool calls this turn. A response with real text is normally a genuine
            # finish. But two cases are non-starts, not completion, and get a bounded nudge:
            #   (a) a degenerate empty response (no text, no calls), and
            #   (b) the model "finishes" having written NO files and called NO tools at all
            #       — i.e. it claimed DONE without doing any work (seen on hard issues).
            produced_nothing = not result.text.strip() and not result.tool_calls
            no_work_done = not executor.written_files and not tool_call_log
            if (produced_nothing or no_work_done) and empty_nudges < _MAX_EMPTY_NUDGES:
                empty_nudges += 1
                messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": "(continuing)"}]}
                )
                messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                if logger:
                    logger.log_iteration(
                        iteration, [{"name": "(no-work nudge)", "input": {}, "output_preview": ""}],
                        {"input": result.input_tokens, "output": result.output_tokens},
                    )
                continue
            if logger:
                logger.log_iteration(
                    iteration, iter_calls,
                    {"input": result.input_tokens, "output": result.output_tokens},
                )
            break

        # Execute each requested tool call and feed results back.
        messages.append({"role": "assistant", "content": result.raw_content})
        tool_results = []
        for call in result.tool_calls:
            output = executor.execute(call)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": output}
            )
            entry = {
                "name": call.name,
                "input": call.input,
                "output_preview": output[:200],
            }
            iter_calls.append(entry)
            tool_call_log.append(entry)

        messages.append({"role": "user", "content": tool_results})

        if logger:
            logger.log_iteration(
                iteration, iter_calls,
                {"input": result.input_tokens, "output": result.output_tokens},
            )

    return iterations_used, tool_call_log


def implement(
    issue: IssueContext,
    repo_ctx: RepoContext,
    plan: FixPlan,
    repo_path: str,
    llm: LLMClient,
    conventions: str,
    settings: Settings,
    logger=None,
) -> ImplementResult:
    project = issue.repo_full_name or "this"
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        max_iterations=settings.max_implement_iterations, project=project
    )
    initial = _build_initial_message(issue, plan, repo_ctx, conventions)
    messages: list[dict] = [{"role": "user", "content": initial}]
    executor = ToolExecutor(repo_path, go_timeout=settings.go_command_timeout_seconds)

    iterations_used, tool_call_log = _run_loop(
        llm,
        system_prompt,
        messages,
        executor,
        settings.max_implement_iterations,
        settings,
        logger,
    )

    return ImplementResult(
        modified_files=list(executor.written_files),
        iterations_used=iterations_used,
        tool_call_log=tool_call_log,
        success=bool(executor.written_files),
        executor=executor,
        messages=messages,
        project=project,
    )


def retry_implementation(
    impl: ImplementResult,
    error_output: str,
    llm: LLMClient,
    settings: Settings,
    repo_path: str,
    logger=None,
    failure_kind: str = "build",
) -> ImplementResult:
    """Re-run the loop with failure context, reusing the prior executor.

    failure_kind selects the verify command and guidance:
      "build"  — a `go build` compile error (verify with go build)
      "checks" — `go vet` and/or a test-binary COMPILE failure (verify with vet + test).
                 NOTE: only mechanical failures reach here; genuine assertion failures are
                 reported as signal and never trigger a retry.
    """
    executor = impl.executor or ToolExecutor(
        repo_path, go_timeout=settings.go_command_timeout_seconds
    )

    modified_contents = "\n\n".join(
        f"### {path}\n```go\n{executor.read_file(path)}\n```"
        for path in executor.written_files
    ) or "(no files written yet)"

    if failure_kind == "checks":
        intro = (
            "Your last changes compile, but `go vet` and/or the test build failed (a "
            "mechanical error such as a vet warning, a syntax error, or an undefined/"
            "mis-typed symbol in a test file). Output:"
        )
        verify = (
            'run_go_command "go vet ./..." and "go test ./...", and output DONE only when '
            "both succeed. Fix the code so the EXISTING tests compile and pass — do NOT "
            "weaken, edit assertions of, skip, or delete tests to silence a failure."
        )
    else:
        intro = "The build failed after your last changes. Compiler output (file:line: message):"
        verify = 'run_go_command "go build ./..." to confirm, and output DONE only when it returns exit 0.'

    retry_message = (
        f"{intro}\n{error_output}\n\n"
        "Current contents of the files you modified:\n"
        f"{modified_contents}\n\n"
        "Fix ONLY what the errors point to, using edit_file for the smallest snippet that "
        "resolves them. Do not rewrite or reformat unrelated code, and preserve all "
        f"backslashes and escape sequences exactly. Then {verify}"
    )
    messages = impl.messages + [{"role": "user", "content": retry_message}]
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        max_iterations=settings.max_implement_iterations, project=impl.project or "this"
    )

    iterations_used, tool_call_log = _run_loop(
        llm,
        system_prompt,
        messages,
        executor,
        settings.max_implement_iterations,
        settings,
        logger,
        tool_call_log=list(impl.tool_call_log),
    )

    return ImplementResult(
        modified_files=list(executor.written_files),
        iterations_used=impl.iterations_used + iterations_used,
        tool_call_log=tool_call_log,
        success=bool(executor.written_files),
        executor=executor,
        messages=messages,
        project=impl.project,
    )
