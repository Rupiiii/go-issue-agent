"""Load ``config/settings.yaml`` into a simple attribute-access object."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "settings.yaml",
)


@dataclass
class Settings:
    provider: str = "google"  # "google" (Gemini) or "anthropic"
    model: str = "gemini-2.5-flash"
    max_tokens_per_call: int = 8192
    thinking_budget: int = 2048  # Gemini thinking tokens per call (0=off, -1=dynamic)
    context_budget_tokens: int = 72000
    max_implement_iterations: int = 10
    max_validate_retries: int = 2
    go_command_timeout_seconds: int = 120
    repo_file_extensions: list[str] = field(default_factory=lambda: [".go"])
    excluded_dirs: list[str] = field(
        default_factory=lambda: ["vendor", "testdata", ".git", "node_modules"]
    )


def load_settings(path: str = _DEFAULT_PATH) -> Settings:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    known = {f.name for f in Settings.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known}
    return Settings(**filtered)
