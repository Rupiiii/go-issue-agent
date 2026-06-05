#!/usr/bin/env python3
"""Run Stage 2 (Understand) in isolation and summarize the RepoContext.

Usage:
    python scripts/run_phase2.py <issue_url> <repo_path>
    python scripts/run_phase2.py        # defaults: validator #1576, /tmp/validator

Exercises the local-clone analysis: file tree, skeletons, relevance scoring, and
full-content loading. Stage 1 runs first only to provide the issue text.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from agent.ingest import ingest
from agent.understand import understand

_DEFAULT_ISSUE = "https://github.com/go-playground/validator/issues/1576"
_DEFAULT_REPO = "/tmp/validator"


def main() -> int:
    load_dotenv()
    url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_ISSUE
    repo = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_REPO

    print(f"Issue : {url}")
    print(f"Repo  : {repo}\n")

    issue = ingest(url)
    ctx = understand(repo, issue)

    tree_lines = ctx.file_tree.splitlines()
    print(f"file_tree     : {len(tree_lines)} entries (showing first 15)")
    for line in tree_lines[:15]:
        print(f"    {line}")

    print(f"\nskeletons     : {len(ctx.skeletons)} files indexed")
    print(f"relevant_files: {ctx.relevant_files}")

    print("\nfull_contents : loaded for these files (line counts):")
    for path, content in ctx.full_contents.items():
        print(f"    {path:24s} {len(content.splitlines())} lines")

    # Show the skeleton of the top-scored relevant file as a sanity check.
    if ctx.relevant_files:
        top = ctx.relevant_files[0]
        sk = ctx.skeletons.get(top, "(no skeleton)")
        print(f"\n--- skeleton preview: {top} (first 12 lines) ---")
        for line in sk.splitlines()[:12]:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
