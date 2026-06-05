#!/usr/bin/env python3
"""CLI entry point for the go-issue-agent.

Usage:
    python main.py --issue <github_issue_url> --repo <local_repo_path> [--output DIR] [--dry-run]

Exits 0 on success, 1 on any unrecoverable error.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic AI contributor for go-playground/validator.")
    parser.add_argument(
        "--issue",
        required=True,
        help="Full GitHub issue URL, e.g. https://github.com/go-playground/validator/issues/123",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute path to a local clone of the target repo.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: ./outputs/<issue_number>/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Stage 4/5 and just produce the plan.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    from agent.settings import load_settings

    provider = load_settings().provider
    if provider == "google":
        llm_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        llm_ok = any(os.environ.get(k) for k in llm_keys)
        llm_hint = "GEMINI_API_KEY (or GOOGLE_API_KEY)"
    else:
        llm_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        llm_hint = "ANTHROPIC_API_KEY"

    missing = []
    if not llm_ok:
        missing.append(llm_hint)
    if not os.environ.get("GITHUB_TOKEN"):
        missing.append("GITHUB_TOKEN")
    if missing:
        print(f"ERROR: missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.example to .env and fill them in.", file=sys.stderr)
        return 1

    args = _parse_args(argv)

    if not os.path.isdir(args.repo):
        print(f"ERROR: repo path does not exist: {args.repo}", file=sys.stderr)
        return 1

    # Import after env validation so a missing dependency message is clearer.
    from agent.pipeline import run

    try:
        run(args.issue, os.path.realpath(args.repo), args.output, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — top-level guard
        print(f"ERROR: pipeline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
