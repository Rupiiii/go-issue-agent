#!/usr/bin/env python3
"""Run Stage 1 (Ingest) in isolation and print the parsed IssueContext.

Usage:
    python scripts/run_phase1.py <github_issue_url>
    python scripts/run_phase1.py          # uses a default validator issue

No LLM and no local clone are involved — this only exercises the GitHub fetch.
"""

from __future__ import annotations

import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from agent.ingest import IngestError, ingest

_DEFAULT_ISSUE = "https://github.com/go-playground/validator/issues/1576"


def main() -> int:
    load_dotenv()
    url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_ISSUE
    print(f"Ingesting: {url}\n")

    try:
        ctx = ingest(url)
    except IngestError as exc:
        print(f"IngestError: {exc}", file=sys.stderr)
        return 1

    print(f"number     : {ctx.number}")
    print(f"repo       : {ctx.repo_full_name}")
    print(f"title      : {ctx.title}")
    print(f"labels     : {ctx.labels}")
    print(f"comments   : {len(ctx.comments)} (bot comments filtered out)")
    print("\n--- body (first 600 chars) ---")
    print(textwrap.shorten(ctx.body or "(empty)", width=600, placeholder=" …"))
    for i, c in enumerate(ctx.comments, 1):
        print(f"\n--- comment {i} (first 300 chars) ---")
        print(textwrap.shorten(c or "(empty)", width=300, placeholder=" …"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
