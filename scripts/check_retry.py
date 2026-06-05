#!/usr/bin/env python3
r"""Deterministically exercise Stage 5's build-failure -> retry path.

Corrupts regexes.go with the classic escape bug (\\d -> \d) so `go build` fails with
'unknown escape', wraps it in an ImplementResult, then calls validate(). Stage 5 should
detect the build failure, call retry_implementation (LLM fixes it), and re-validate.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from agent.implement import ImplementResult
from agent.llm import LLMClient
from agent.logger import get_logger
from agent.settings import load_settings
from agent.tools import ToolExecutor
from agent.validate import run_go, validate

REPO = sys.argv[1] if len(sys.argv) > 1 else "/tmp/validator"


def main() -> int:
    load_dotenv()
    settings = load_settings()
    logger = get_logger("retry-check")

    # 1) Corrupt a real string literal in regexes.go to break the build.
    ex = ToolExecutor(REPO, go_timeout=settings.go_command_timeout_seconds)
    before = ex.read_file("regexes.go")
    # einRegexString = "^(\\d{2}-\\d{7})$"  ->  break the escapes
    bad = ex.edit_file(
        "regexes.go",
        r'einRegexString                   = "^(\\d{2}-\\d{7})$"',
        r'einRegexString                   = "^(\d{2}-\d{7})$"',  # invalid Go escapes
    )
    print("inject:", bad)
    ok, out = run_go("go build ./...", REPO, settings.go_command_timeout_seconds)
    print(f"baseline build after corruption: ok={ok} (expected False)")
    print("  " + out.strip().split(chr(10))[0] if out.strip() else "")

    # 2) Wrap as an ImplementResult, mimicking a Stage-4 run that just wrote this file.
    impl = ImplementResult(
        modified_files=["regexes.go"],
        iterations_used=1,
        tool_call_log=[],
        success=True,
        executor=ex,
        messages=[{"role": "user", "content": "Implement the fix in regexes.go."}],
    )

    # 3) Validate -> should retry implement on build failure, LLM fixes, re-validate.
    llm = LLMClient(
        settings.model, settings.max_tokens_per_call, settings.provider, settings.thinking_budget
    )
    logger.stage("validate")
    result = validate(REPO, impl, llm, settings, logger)
    logger.done("retry-check")

    print("\n=== ValidationResult ===")
    print(f"  build_passed : {result.build_passed}")
    print(f"  vet_passed   : {result.vet_passed}")
    print(f"  test_passed  : {result.test_passed}")
    print(f"  retries_used : {result.retries_used}  (expected >= 1)")
    if result.errors:
        print("  errors:", [e[:80] for e in result.errors])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
