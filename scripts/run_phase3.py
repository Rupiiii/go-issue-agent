#!/usr/bin/env python3
"""Run Stages 1-3 and print the FixPlan (first LLM-backed phase).

Usage:
    python scripts/run_phase3.py <issue_url> <repo_path>
    python scripts/run_phase3.py        # defaults: validator #1576, /tmp/validator

Uses the LLM provider configured in config/settings.yaml (default: Gemini).
Requires the corresponding API key in .env (GEMINI_API_KEY for Google).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from agent.ingest import ingest
from agent.llm import LLMClient
from agent.plan import load_conventions, plan_fix
from agent.settings import load_settings
from agent.understand import understand

_DEFAULT_ISSUE = "https://github.com/go-playground/validator/issues/1576"
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
    repo_ctx = understand(repo, issue, settings)
    llm = LLMClient(
        settings.model, settings.max_tokens_per_call, settings.provider, settings.thinking_budget
    )
    conventions = load_conventions()

    plan = plan_fix(issue, repo_ctx, conventions, llm)

    print("=== FixPlan ===")
    print(json.dumps(asdict(plan), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
