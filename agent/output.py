"""Stage 6 — Output.

Create a branch, commit the changes, write the unified diff, ask the LLM for a PR
summary, and emit a ``run_summary.json`` with metadata. All artifacts land in
``output_dir``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict

from .implement import ImplementResult
from .ingest import IssueContext
from .llm import LLMClient
from .plan import FixPlan
from .validate import ValidationResult

_PR_SYSTEM_PROMPT = """\
You are writing a pull request description for a Go open-source project.
Write clearly and concisely. Base every statement on the diff and validation result you
are given — do not invent changes or claim tests pass if the validation result says they
did not. Follow this structure exactly:
1. One-line summary (becomes the PR title)
2. ## What this PR does (2-3 sentences)
3. ## Changes made (bullet list of files changed and why)
4. ## Testing (state what tests were added/modified AND the actual build/test/vet result)
5. ## Related issue (just: "Closes #{number}")"""


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _commit_changes(repo_path: str, issue: IssueContext, agent_files: list[str]) -> tuple[str, bool]:
    """Returns (branch_name, committed). committed is False if the agent wrote no files."""
    branch_name = f"agent/fix-issue-{issue.number}"
    # Create (or reset to) the branch.
    created = _git(["checkout", "-b", branch_name], repo_path)
    if created.returncode != 0:
        _git(["checkout", branch_name], repo_path)

    title = issue.title[:60]
    committed = False
    if agent_files:
        # Stage ONLY the files the agent actually wrote. `go fmt ./...` (run in Stages 4
        # and 5) reformats the whole module on disk, including files the agent never
        # touched (e.g. translations/ko/ko_test.go). Staging just the agent's files keeps
        # that noise out of the commit and the diff.
        _git(["add", "--", *agent_files], repo_path)
        result = _git(["commit", "-m", f"fix: address issue #{issue.number} - {title}"], repo_path)
        committed = result.returncode == 0

    # Discard any remaining unstaged changes (the spurious whole-module reformatting) so
    # the working tree — and `git diff HEAD~1 HEAD` — reflect only the agent's work.
    _git(["checkout", "--", "."], repo_path)
    return branch_name, committed


def _generate_diff(repo_path: str, committed: bool, agent_files: list[str]) -> str:
    # If the agent committed nothing, there is no diff — never fall back to HEAD~1..HEAD,
    # which would surface an unrelated commit from the repo's own history.
    if not committed:
        return "(no changes — the agent did not modify any files)\n"
    result = _git(["diff", "HEAD~1", "HEAD", "--", *agent_files], repo_path)
    return result.stdout


def _generate_pr_summary(
    issue: IssueContext,
    plan: FixPlan,
    validation: ValidationResult,
    diff_text: str,
    llm: LLMClient,
    logger=None,
) -> str:
    system = _PR_SYSTEM_PROMPT.replace("{number}", str(issue.number))
    user = f"""Issue title: {issue.title}
Issue body: {issue.body}
Fix plan reasoning: {plan.reasoning}
Diff:
{diff_text[:12000]}
Validation result: build={validation.build_passed}, tests={validation.test_passed}, vet={validation.vet_passed}"""
    result = llm.complete(
        system=system, messages=[{"role": "user", "content": user}], temperature=0.3
    )
    if logger:
        logger.add_tokens(result.input_tokens, result.output_tokens)
    return result.text


def write_output(
    issue: IssueContext,
    plan: FixPlan,
    validation: ValidationResult,
    impl: ImplementResult,
    repo_path: str,
    output_dir: str,
    llm: LLMClient,
    logger=None,
) -> None:
    os.makedirs(output_dir, exist_ok=True)

    # Step A — git ops (commit only the agent's files; see _commit_changes).
    branch_name, committed = _commit_changes(repo_path, issue, impl.modified_files)

    # Step B — diff (agent's files only; empty if nothing was committed).
    diff_text = _generate_diff(repo_path, committed, impl.modified_files)
    with open(os.path.join(output_dir, "changes.diff"), "w", encoding="utf-8") as f:
        f.write(diff_text)

    # Step C — PR summary.
    pr_summary = _generate_pr_summary(issue, plan, validation, diff_text, llm, logger)
    with open(os.path.join(output_dir, "pr_summary.md"), "w", encoding="utf-8") as f:
        f.write(pr_summary)

    # Step D — run summary.
    total_tokens = (
        dict(logger.record["total_tokens"]) if logger else {"input": 0, "output": 0}
    )
    estimated_cost = logger.estimated_cost() if logger else 0.0
    run_summary = {
        "issue_url": issue.url,
        "branch": branch_name,
        "files_modified": impl.modified_files,
        "validation": {
            "build": validation.build_passed,
            "tests": validation.test_passed,
            "vet": validation.vet_passed,
        },
        "iterations_used": impl.iterations_used,
        "total_api_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost,
    }
    with open(os.path.join(output_dir, "run_summary.json"), "w", encoding="utf-8") as f:
        json.dump(run_summary, f, indent=2)
