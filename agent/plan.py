"""Stage 3 — Plan.

Ask the LLM to analyze the issue and emit a strict-JSON ``FixPlan``. If the first
response is not valid JSON, make one repair call; if that also fails, raise
``PlanError``. A ``large`` complexity estimate is logged as a warning but does not
abort the run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .ingest import IssueContext
from .llm import LLMClient
from .understand import RepoContext

_CONVENTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "validator_conventions.md",
)


class PlanError(Exception):
    """Raised when a valid FixPlan cannot be produced."""


@dataclass
class FixPlan:
    files_to_modify: list[str] = field(default_factory=list)
    files_to_create: list[str] = field(default_factory=list)
    change_descriptions: list[str] = field(default_factory=list)
    conventions_to_follow: list[str] = field(default_factory=list)
    reasoning: str = ""
    estimated_complexity: str = "medium"  # "small" | "medium" | "large"


_SYSTEM_PROMPT = """\
You are a senior Go engineer working on the go-playground/validator library.
Analyze a GitHub issue and produce a precise, MINIMAL implementation plan.

Guidelines:
- Choose the smallest set of files and changes that fix the issue. Each change will be
  applied by rewriting the whole file, so prefer edits localized to a single validator
  function over rewriting large regex or data tables.
- Identify the existing built-in validator most similar to what the issue needs and follow
  its pattern.
- Be concrete in change_descriptions: name the exact function(s), tag(s), and test(s) to
  touch, not just the file.

PANIC ISSUES: If the issue includes a panic stack trace, read it carefully. The fix site is
the first APPLICATION-code frame in the trace — not the stdlib frame (e.g. strconv,
runtime) and not the outermost function. Name the exact file, function, and approximate
line number in your plan, and the specific call/branch that triggers the panic (e.g. an
asInt() call inside a particular reflect.Kind case). Do not infer the fix location from the
function name alone.

Output ONLY valid JSON matching the FixPlan schema. Do not include any other text."""

_SCHEMA_HINT = """\
The JSON object must have exactly these keys:
{
  "files_to_modify": [string],
  "files_to_create": [string],
  "change_descriptions": [string],
  "conventions_to_follow": [string],
  "reasoning": string,
  "estimated_complexity": "small" | "medium" | "large"
}"""


def load_conventions(path: str = _CONVENTIONS_PATH) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_user_message(issue: IssueContext, repo_ctx: RepoContext, conventions: str) -> str:
    skeletons = "\n\n".join(
        repo_ctx.skeletons.get(f, "") for f in repo_ctx.relevant_files
    )
    full = "\n\n".join(
        f"### {path}\n```go\n{content}\n```"
        for path, content in repo_ctx.full_contents.items()
    )
    comments = "\n\n".join(issue.comments) if issue.comments else "(none)"
    return f"""## Issue
Title: {issue.title}
Body: {issue.body}

## Comments
{comments}

## Repository conventions
{conventions}

## Repository file tree
{repo_ctx.file_tree}

## Relevant file skeletons
{skeletons}

## Full file contents
{full}

## Task
Produce a FixPlan JSON object. Be specific about which files to modify and why.
Identify which existing validators are most similar to what this issue requests.

{_SCHEMA_HINT}"""


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of model text, tolerating ```json fences."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0)
    return json.loads(cleaned)


def _to_plan(data: dict) -> FixPlan:
    return FixPlan(
        files_to_modify=list(data.get("files_to_modify", [])),
        files_to_create=list(data.get("files_to_create", [])),
        change_descriptions=list(data.get("change_descriptions", [])),
        conventions_to_follow=list(data.get("conventions_to_follow", [])),
        reasoning=str(data.get("reasoning", "")),
        estimated_complexity=str(data.get("estimated_complexity", "medium")),
    )


def plan_fix(
    issue: IssueContext,
    repo_ctx: RepoContext,
    conventions: str,
    llm: LLMClient,
    logger=None,
) -> FixPlan:
    user_message = _build_user_message(issue, repo_ctx, conventions)
    messages = [{"role": "user", "content": user_message}]

    result = llm.complete(system=_SYSTEM_PROMPT, messages=messages, temperature=0.2)
    if logger:
        logger.add_tokens(result.input_tokens, result.output_tokens)

    try:
        data = _extract_json(result.text)
    except (json.JSONDecodeError, ValueError):
        # One repair attempt.
        repair_messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": result.text},
            {
                "role": "user",
                "content": (
                    "That was not valid JSON. Respond again with ONLY the JSON object, "
                    f"no prose, no code fences.\n\n{_SCHEMA_HINT}"
                ),
            },
        ]
        repair = llm.complete(system=_SYSTEM_PROMPT, messages=repair_messages, temperature=0.0)
        if logger:
            logger.add_tokens(repair.input_tokens, repair.output_tokens)
        try:
            data = _extract_json(repair.text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlanError(f"LLM did not produce valid FixPlan JSON: {exc}") from exc

    plan = _to_plan(data)

    if plan.estimated_complexity == "large" and logger:
        logger.warning("FixPlan estimated_complexity is 'large' — attempting anyway.")

    return plan
