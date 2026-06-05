#!/usr/bin/env python3
"""Run Stages 1-4 and report the implementation result (tool-calling loop).

Usage:
    python scripts/run_phase4.py <issue_url> <repo_path>
    python scripts/run_phase4.py        # defaults: validator #1561, /tmp/validator

Runs Stage 1-3 to get a plan, then drives the Stage 4 tool loop. Prints the
per-call trace and the files the agent wrote. Uses a RunLogger so token usage is
captured. Does NOT run Stage 5/6.
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
    print("=== Plan: files_to_modify ===")
    print(f"  {plan.files_to_modify}\n")

    logger.stage("implement")
    impl = implement(issue, repo_ctx, plan, repo, llm, conventions, settings, logger)
    logger.log(impl)
    logger.done("phase4-only")

    print("=== Stage 4 tool-call trace ===")
    for i, call in enumerate(impl.tool_call_log, 1):
        preview = call["output_preview"].replace("\n", " ")[:90]
        inp = {k: (v[:50] + "…" if isinstance(v, str) and len(v) > 50 else v)
               for k, v in call["input"].items()}
        print(f"  {i:2d}. {call['name']:16s} {inp}")
        print(f"      -> {preview}")

    print("\n=== Result ===")
    print(f"  iterations_used : {impl.iterations_used}")
    print(f"  modified_files  : {impl.modified_files}")
    print(f"  success         : {impl.success}")
    print(f"  tokens          : {logger.record['total_tokens']}  (~${logger.estimated_cost():.4f})")
    print(f"  log             : {logger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
