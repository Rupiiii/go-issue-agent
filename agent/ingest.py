"""Stage 1 — Ingest.

Fetch a GitHub issue (title, body, comments, labels) via PyGithub and return an
``IssueContext``. Fails fast: a missing issue or bad token raises ``IngestError``
with no retry.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from github import Auth, Github
from github.GithubException import GithubException


class IngestError(Exception):
    """Raised when the issue cannot be fetched."""


@dataclass
class IssueContext:
    number: int
    title: str
    body: str
    comments: list[str] = field(default_factory=list)  # body of each comment, in order
    labels: list[str] = field(default_factory=list)
    repo_full_name: str = ""                            # e.g. "go-playground/validator"
    url: str = ""


_ISSUE_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, issue_number) from a GitHub issue URL."""
    match = _ISSUE_URL_RE.search(url)
    if not match:
        raise IngestError(f"could not parse GitHub issue URL: {url}")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def ingest(issue_url: str, github_token: str | None = None) -> IssueContext:
    token = github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise IngestError("GITHUB_TOKEN is not set")

    owner, repo, issue_number = parse_issue_url(issue_url)
    repo_full_name = f"{owner}/{repo}"

    try:
        gh = Github(auth=Auth.Token(token))
        repository = gh.get_repo(repo_full_name)
        issue = repository.get_issue(number=issue_number)

        comments: list[str] = []
        for comment in issue.get_comments():
            login = (comment.user.login if comment.user else "") or ""
            if "[bot]" in login:  # filter out bot comments
                continue
            comments.append(comment.body or "")
            time.sleep(1)  # be gentle with the comments API to avoid rate limits

        labels = [label.name for label in issue.labels]

        return IssueContext(
            number=issue_number,
            title=issue.title or "",
            body=issue.body or "",
            comments=comments,
            labels=labels,
            repo_full_name=repo_full_name,
            url=issue_url,
        )
    except GithubException as exc:
        raise IngestError(
            f"failed to fetch issue {repo_full_name}#{issue_number}: {exc}"
        ) from exc
