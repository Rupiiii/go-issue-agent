#!/usr/bin/env python3
"""Run Stages 1-5 and report the ValidationResult (build/vet/test + retries).

Usage:
    python scripts/run_phase5.py <issue_url> <repo_path>
    python scripts/run_phase5.py        # defaults: validator #1543, /tmp/validator

Runs ingest -> understand -> plan -> implement -> validate. Stage 5 runs
go fmt/build/vet/test, retries Stage 4 ONLY on build failure (up to
max_validate_retries), and reports test failures as signal (never hidden).
Does NOT run Stage 6 (git branch / PR summary).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from agent.implement import implement
from agent.ingest import ingest
from agent.llm import LLMClient
from agent.logger import get_logger
from agent.plan import load_conventions, plan_fix
from agent.settings import load_settings
from agent.understand import understand
from agent.validate import validate

_DEFAULT_ISSUE = "https://github.com/go-playground/validator/issues/1543"
_DEFAULT_REPO = "/tmp/validator"


def main() -> int:
    load_dotenv()
    url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_ISSUE
    repo = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_REPO

    settings = load_settings()
    print(f"Provider : {settings.provider}  |  Model: {settings.model}")
    print(f"Issue    : {url}")
    print(f"Repo     : {repo}\n")

    issue = ingest(url)
    logger = get_logger(issue.number, model=settings.model)
    repo_ctx = understand(repo, issue, settings)
    llm = LLMClient(
        settings.model, settings.max_tokens_per_call, settings.provider, settings.thinking_budget
    )
    conventions = load_conventions()

    plan = plan_fix(issue, repo_ctx, conventions, llm, logger)
    print(f"=== Plan: files_to_modify ===\n  {plan.files_to_modify}\n")

    logger.stage("implement")
    impl = implement(issue, repo_ctx, plan, repo, llm, conventions, settings, logger)
    logger.log(impl)
    print(f"=== Stage 4 result ===\n  modified: {impl.modified_files}  iterations: {impl.iterations_used}\n")

    logger.stage("validate")
    result = validate(repo, impl, llm, settings, logger)
    logger.log(result)
    logger.done("phase5-only")

    print("=== Stage 5: ValidationResult ===")
    print(f"  fmt_applied  : {result.fmt_applied}")
    print(f"  build_passed : {result.build_passed}")
    print(f"  vet_passed   : {result.vet_passed}")
    print(f"  test_passed  : {result.test_passed}")
    print(f"  retries_used : {result.retries_used}")
    if result.errors:
        print("\n=== Errors (first 1500 chars each) ===")
        for err in result.errors:
            print("  " + err[:1500].replace("\n", "\n  "))
    print(f"\n  tokens: {logger.record['total_tokens']}   log: {logger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
