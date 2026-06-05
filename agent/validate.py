"""Stage 5 — Validate.

Run the Go toolchain over the modified repo: ``go fmt`` then ``go build``, ``go vet``,
``go test``. Re-run Stage 4 (up to ``max_validate_retries`` times) on MECHANICAL failures —
a ``go build`` error, a ``go vet`` error, or a test binary that fails to COMPILE — because
those are fixable without gaming anything. Genuine test ASSERTION failures are NOT retried:
they are honest signal about whether the fix is correct, and retrying "until green" would
just pressure the model to weaken or delete tests. Failures are surfaced, never hidden.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from .implement import ImplementResult, retry_implementation
from .llm import LLMClient
from .settings import Settings


def _is_test_compile_failure(output: str) -> bool:
    """True if `go test` failed because the test binary did not COMPILE (mechanical),
    as opposed to a test running and failing an assertion (logical signal).

    Go prints "[build failed]" when test compilation fails, and compiler-style
    "file_test.go:line:col:" errors; a real assertion failure instead prints "--- FAIL:".
    """
    if "--- FAIL:" in output:
        return False  # a test actually ran and failed -> logical
    if "[build failed]" in output:
        return True
    return bool(re.search(r"_test\.go:\d+:\d+:", output))


@dataclass
class ValidationResult:
    build_passed: bool = False
    test_passed: bool = False
    vet_passed: bool = False
    fmt_applied: bool = False
    errors: list[str] = field(default_factory=list)
    retries_used: int = 0


def run_go(command: str, cwd: str, timeout: int = 120) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command.split(),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "ERROR: the 'go' toolchain is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return False, f"ERROR: '{command}' timed out after {timeout}s."
    return result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def validate(
    repo_path: str,
    impl_result: ImplementResult,
    llm_client: LLMClient,
    settings: Settings,
    logger=None,
) -> ValidationResult:
    timeout = settings.go_command_timeout_seconds
    max_retries = settings.max_validate_retries
    impl = impl_result
    retries_used = 0

    for attempt in range(max_retries + 1):
        # Step 1 — format (best effort).
        fmt_ok, _ = run_go("go fmt ./...", repo_path, timeout)

        # Step 2 — build.
        build_ok, build_output = run_go("go build ./...", repo_path, timeout)
        if not build_ok:
            if attempt < max_retries:
                if logger:
                    logger.warning("build failed (attempt %d) — retrying Stage 4", attempt + 1)
                impl = retry_implementation(
                    impl, build_output, llm_client, settings, repo_path, logger
                )
                retries_used += 1
                continue
            return ValidationResult(
                build_passed=False,
                test_passed=False,
                vet_passed=False,
                fmt_applied=fmt_ok,
                errors=[f"build:\n{build_output}"],
                retries_used=retries_used,
            )

        # Step 3 — vet.
        vet_ok, vet_output = run_go("go vet ./...", repo_path, timeout)

        # Step 4 — test.
        test_ok, test_output = run_go("go test ./...", repo_path, timeout)

        # Classify failures. MECHANICAL = vet error or a test binary that won't compile;
        # these are fixable and get retried. A test that ran and failed an assertion is a
        # LOGICAL signal — never retried (that would only pressure the model to game tests).
        test_compile_failed = (not test_ok) and _is_test_compile_failure(test_output)
        mechanical: list[str] = []
        if not vet_ok:
            mechanical.append(f"go vet:\n{vet_output}")
        if test_compile_failed:
            mechanical.append(f"go test (build failed):\n{test_output}")

        if mechanical and attempt < max_retries:
            if logger:
                logger.warning(
                    "mechanical check failure (vet/test-compile) — retrying Stage 4 (attempt %d)",
                    attempt + 1,
                )
            impl = retry_implementation(
                impl, "\n\n".join(mechanical), llm_client, settings, repo_path, logger,
                failure_kind="checks",
            )
            retries_used += 1
            continue

        # No retryable mechanical failure remains (or retries exhausted). Report honestly;
        # a logical test (assertion) failure is left as signal.
        errors: list[str] = []
        if not vet_ok:
            errors.append(f"vet:\n{vet_output}")
        if not test_ok:
            errors.append(f"test:\n{test_output}")

        return ValidationResult(
            build_passed=True,
            test_passed=test_ok,
            vet_passed=vet_ok,
            fmt_applied=fmt_ok,
            errors=errors,
            retries_used=retries_used,
        )

    # Unreachable, but keeps type checkers happy.
    return ValidationResult(retries_used=retries_used)
