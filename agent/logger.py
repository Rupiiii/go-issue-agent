"""Structured JSON logger.

Every run writes a single JSON file to ``logs/run_{issue_number}_{timestamp}.json``
capturing each stage's input/output summary, per-iteration tool calls in Stage 4,
and a final token/cost tally. A thin wrapper over the stdlib ``logging`` module is
used for human-readable console output; the structured record is built up in memory
and flushed to disk at the end of the run (and on failure).

Keep log entries small: store output *previews* (first 200 chars), not full content.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

# Approximate list prices (USD per 1M tokens) for the cost estimate only. Keyed by a
# substring of the model id, checked MOST-SPECIFIC FIRST. Output rate covers completion
# (and, for Gemini thinking models, thinking) tokens. Update if vendor pricing changes.
_PRICING: list[tuple[str, float, float]] = [
    ("gemini-2.5-flash-lite", 0.10, 0.40),
    ("gemini-2.5-flash", 0.30, 2.50),
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.0-flash", 0.10, 0.40),
    ("claude-opus", 15.0, 75.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 0.80, 4.0),
]
# Fallback when the model id matches nothing above (assume a cheap flash-class model).
_DEFAULT_RATES = (0.30, 2.50)

_PREVIEW_CHARS = 200


def _rates_for_model(model: str | None) -> tuple[float, float]:
    """Return (input_rate, output_rate) per 1M tokens for a model id."""
    if model:
        for key, in_rate, out_rate in _PRICING:
            if key in model:
                return in_rate, out_rate
    return _DEFAULT_RATES


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: Any) -> Any:
    """Shrink a value for logging: truncate long strings, recurse into containers."""
    if isinstance(value, str):
        if len(value) > _PREVIEW_CHARS:
            return value[:_PREVIEW_CHARS] + f"... [+{len(value) - _PREVIEW_CHARS} chars]"
        return value
    if isinstance(value, dict):
        return {k: _preview(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_preview(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _preview(v) for k, v in dataclasses.asdict(value).items()}
    return value


class RunLogger:
    """Accumulates a structured record for one pipeline run."""

    def __init__(self, issue_number: int | str, log_dir: str = "logs", model: str | None = None):
        self.issue_number = issue_number
        self.model = model
        self._input_rate, self._output_rate = _rates_for_model(model)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"issue-{issue_number}-{ts}"

        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"run_{issue_number}_{ts}.json")

        self.record: dict[str, Any] = {
            "run_id": self.run_id,
            "model": model,
            "started_at": _utcnow(),
            "stages": [],
            "total_tokens": {"input": 0, "output": 0},
            "estimated_cost_usd": 0.0,
            "final_status": "running",
        }
        self._current_stage: dict[str, Any] | None = None
        self._stage_start: float | None = None

        # Console logger.
        self._log = logging.getLogger(self.run_id)
        if not self._log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self._log.addHandler(handler)
            self._log.setLevel(logging.INFO)
        self._log.propagate = False

    # ----- stage lifecycle -------------------------------------------------

    def stage(self, name: str) -> None:
        """Begin a new stage. Finalizes the previous stage's timing if open."""
        self._close_stage()
        self._stage_start = time.monotonic()
        self._current_stage = {
            "stage": name,
            "started_at": _utcnow(),
        }
        self.record["stages"].append(self._current_stage)
        self._log.info("=== stage: %s ===", name)

    def _close_stage(self) -> None:
        if self._current_stage is not None and self._stage_start is not None:
            self._current_stage["completed_at"] = _utcnow()
            self._current_stage["duration_ms"] = round((time.monotonic() - self._stage_start) * 1000)

    def log(self, output: Any) -> None:
        """Attach an output summary (previewed) to the current stage."""
        if self._current_stage is None:
            return
        self._current_stage["output_summary"] = _preview(output)

    # ----- Stage 4 iterations ---------------------------------------------

    def log_iteration(self, iteration: int, tool_calls: list[dict], tokens: dict[str, int]) -> None:
        """Record one tool-calling loop iteration under the current stage."""
        if self._current_stage is None:
            return
        self._current_stage.setdefault("iterations", []).append(
            {
                "iteration": iteration,
                "tool_calls": _preview(tool_calls),
                "tokens": tokens,
            }
        )
        self._log.debug("iteration %d: %d tool call(s)", iteration, len(tool_calls))

    # ----- token accounting -----------------------------------------------

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.record["total_tokens"]["input"] += input_tokens
        self.record["total_tokens"]["output"] += output_tokens

    def estimated_cost(self) -> float:
        t = self.record["total_tokens"]
        return round(
            t["input"] / 1_000_000 * self._input_rate
            + t["output"] / 1_000_000 * self._output_rate,
            4,
        )

    # ----- plain passthroughs ---------------------------------------------

    def info(self, msg: str, *args: Any) -> None:
        self._log.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._log.warning(msg, *args)

    def debug(self, msg: str, *args: Any) -> None:
        self._log.debug(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self._log.error(msg, *args)

    # ----- finalization ----------------------------------------------------

    def done(self, status: str = "success") -> None:
        self._close_stage()
        self.record["final_status"] = status
        self.record["completed_at"] = _utcnow()
        self.record["estimated_cost_usd"] = self.estimated_cost()
        self.flush()
        self._log.info("run complete: %s (cost ~$%.4f)", status, self.record["estimated_cost_usd"])

    def flush(self) -> None:
        """Write the current record to disk. Safe to call repeatedly."""
        self.record["estimated_cost_usd"] = self.estimated_cost()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.record, f, indent=2, default=str)


def get_logger(issue_number: int | str, log_dir: str = "logs", model: str | None = None) -> RunLogger:
    return RunLogger(issue_number, log_dir, model)
