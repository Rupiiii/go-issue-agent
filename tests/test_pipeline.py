"""Smoke tests that run without network or API access.

These exercise the deterministic, offline parts of the pipeline: URL parsing,
settings loading, tool path-safety, repo understanding on a tiny fixture, and the
plan JSON extraction helper. Stages that require the Anthropic API or GitHub are
not covered here (run those end-to-end via main.py).

Run: python -m pytest tests/ -q     (or: python tests/test_pipeline.py)
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.ingest import IssueContext, parse_issue_url  # noqa: E402
from agent.plan import _extract_json  # noqa: E402
from agent.settings import load_settings  # noqa: E402
from agent.tools import ToolExecutor  # noqa: E402
from agent.understand import understand  # noqa: E402


def test_parse_issue_url():
    owner, repo, num = parse_issue_url(
        "https://github.com/go-playground/validator/issues/1122"
    )
    assert owner == "go-playground"
    assert repo == "validator"
    assert num == 1122


def test_load_settings():
    s = load_settings()
    assert s.provider in ("google", "anthropic")
    assert s.model  # non-empty
    assert s.max_implement_iterations > 0
    assert ".go" in s.repo_file_extensions


def test_tool_path_escape_blocked():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        result = ex.read_file("../../etc/passwd")
        assert result.startswith("ERROR")


def test_tool_write_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        assert ex.write_file("foo.go", "package foo\n").startswith("OK")
        assert ex.read_file("foo.go") == "package foo\n"
        assert "foo.go" in ex.written_files


def test_run_go_command_rejects_non_whitelisted():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        assert ex.run_go_command("go run main.go").startswith("ERROR")


def test_read_file_line_range():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        ex.write_file("big.go", "".join(f"line {i}\n" for i in range(1, 5001)))
        out = ex.read_file("big.go", 4998, 5000)
        assert "line 4998" in out and "line 5000" in out and "line 1\n" not in out


def test_edit_file_unique_replace():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        ex.write_file("x.go", "package x\nfunc A(){}\nfunc B(){}\n")
        assert ex.edit_file("x.go", "func B(){}", "func B(){ return }").startswith("OK")
        assert "func B(){ return }" in ex.read_file("x.go")


def test_edit_file_non_unique_and_missing():
    with tempfile.TemporaryDirectory() as d:
        ex = ToolExecutor(d)
        ex.write_file("y.go", "a\na\n")
        assert "not unique" in ex.edit_file("y.go", "a", "z")
        assert "not found" in ex.edit_file("y.go", "nope", "z")


def test_test_failure_classification():
    from agent.validate import _is_test_compile_failure
    # Mechanical: test binary fails to compile -> retryable
    assert _is_test_compile_failure("FAIL\tpkg [build failed]")
    assert _is_test_compile_failure("./validator_test.go:10:5: too many arguments")
    # Logical: a test ran and failed an assertion -> signal, NOT retried
    assert not _is_test_compile_failure(
        "--- FAIL: TestX (0.00s)\n    validator_test.go:4056: Index 0\nFAIL\tpkg\t0.7s"
    )


def test_understand_on_fixture():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "baked_in.go"), "w") as f:
            f.write("package validator\n\nfunc hasMin(fl FieldLevel) bool { return true }\n")
        with open(os.path.join(d, "baked_in_test.go"), "w") as f:
            f.write("package validator\n\nfunc TestMin(t *testing.T) {}\n")
        issue = IssueContext(number=1, title="add min validator", body="need a min tag")
        ctx = understand(d, issue)
        assert "baked_in.go" in ctx.relevant_files
        assert "baked_in_test.go" in ctx.relevant_files
        assert "baked_in.go" in ctx.full_contents


def test_extract_json_with_fence():
    data = _extract_json('```json\n{"estimated_complexity": "small"}\n```')
    assert data["estimated_complexity"] == "small"


def test_extract_json_bare():
    data = _extract_json('Here you go: {"reasoning": "x", "files_to_modify": []}')
    assert data["files_to_modify"] == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
