"""Pipeline orchestration — runs all six stages in order.

Handles git hygiene before implementation and guarantees the repo is never left in
a broken state: on an unrecoverable error during Stage 4/5 the working tree is
reset and the error re-raised.
"""

from __future__ import annotations

import os
import subprocess

from .implement import implement
from .ingest import ingest
from .llm import LLMClient
from .logger import get_logger
from .output import write_output
from .plan import load_conventions, plan_fix
from .settings import load_settings
from .understand import understand
from .validate import validate


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _git_hygiene(repo_path: str, logger) -> None:
    """Best-effort: stash local changes and refresh the default branch.

    None of these are fatal — a missing remote or detached HEAD should not abort
    a run, so failures are logged and ignored.
    """
    _git(["stash"], repo_path)
    # Detect default branch (validator uses 'master'); fall back to 'main'.
    head = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_path)
    branch = "main"
    if head.returncode == 0 and head.stdout.strip():
        branch = head.stdout.strip().rsplit("/", 1)[-1]
    co = _git(["checkout", branch], repo_path)
    if co.returncode != 0:
        logger.warning("git hygiene: could not checkout %s (%s)", branch, co.stderr.strip())
        return
    pull = _git(["pull", "origin", branch], repo_path)
    if pull.returncode != 0:
        logger.warning("git hygiene: pull failed (offline?) — continuing with local state")


def _recover_repo(repo_path: str) -> None:
    _git(["checkout", "--", "."], repo_path)
    _git(["clean", "-fd"], repo_path)


def run(issue_url: str, repo_path: str, output_dir: str | None = None, dry_run: bool = False):
    settings = load_settings()

    # Stage 1 — Ingest (also gives us the issue number for the logger/output paths).
    issue = ingest(issue_url)

    logger = get_logger(issue.number, model=settings.model)
    logger.stage("ingest")
    logger.log(issue)
    logger.info("ingested issue #%d: %s", issue.number, issue.title)

    if output_dir is None:
        output_dir = os.path.join("outputs", str(issue.number))

    llm = LLMClient(
        model=settings.model,
        max_tokens=settings.max_tokens_per_call,
        provider=settings.provider,
        thinking_budget=settings.thinking_budget,
    )

    # Stage 2 — Understand.
    logger.stage("understand")
    repo_ctx = understand(repo_path, issue, settings)
    logger.log(repo_ctx)
    logger.info("relevant files: %s", repo_ctx.relevant_files)

    # Stage 3 — Plan.
    logger.stage("plan")
    conventions = load_conventions()
    plan = plan_fix(issue, repo_ctx, conventions, llm, logger)
    logger.log(plan)
    logger.flush()

    if dry_run:
        logger.info("dry-run: skipping Stage 4/5. Plan reasoning:\n%s", plan.reasoning)
        logger.done("dry-run")
        return plan

    # Git hygiene before touching files.
    _git_hygiene(repo_path, logger)

    try:
        # Stage 4 — Implement.
        logger.stage("implement")
        impl = implement(issue, repo_ctx, plan, repo_path, llm, conventions, settings, logger)
        logger.log(impl)
        logger.flush()

        # Stage 5 — Validate.
        logger.stage("validate")
        validation = validate(repo_path, impl, llm, settings, logger)
        logger.log(validation)
        logger.flush()
    except Exception as exc:
        logger.error("unrecoverable error during implement/validate: %s", exc)
        _recover_repo(repo_path)
        logger.done("error")
        raise

    # Stage 6 — Output.
    logger.stage("output")
    write_output(issue, plan, validation, impl, repo_path, output_dir, llm, logger)
    logger.log({"output_dir": output_dir})

    status = "success" if validation.build_passed else "build-failed"
    logger.done(status)
    return validation
