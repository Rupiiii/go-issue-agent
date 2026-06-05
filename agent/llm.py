"""LLM client abstraction.

The rest of the codebase talks to the model exclusively through ``LLMClient`` and
never imports a vendor SDK directly, so the backend stays swappable. ``LLMClient``
dispatches to a provider backend (``google`` or ``anthropic``) selected by config.

The *canonical* message format used everywhere else in the codebase is
Anthropic-shaped:

  - user text:      {"role": "user", "content": "<str>"}
  - assistant turn: {"role": "assistant", "content": [ {type:"text",...},
                                                        {type:"tool_use", id, name, input} ]}
  - tool results:   {"role": "user", "content": [ {type:"tool_result",
                                                    tool_use_id, content} ]}

Each backend adapts this canonical shape to/from its own wire format, and always
returns ``raw_content`` in the canonical shape so the tool-calling loop in
implement.py can append it verbatim regardless of provider.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Raised on any unrecoverable LLM/API failure."""


# Substrings that mark a transient, retryable API failure (overload / rate limit /
# server blip) across both providers.
_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "overloaded",
    "429", "RESOURCE_EXHAUSTED", "rate limit", "rate_limit",
    "500", "INTERNAL", "502", "504", "deadline",
)
_MAX_TRANSIENT_RETRIES = 6
_TRANSIENT_BACKOFF_BASE = 2.0  # seconds; doubles each retry (2,4,8,16,32,64 ≈ 2 min total)


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m.lower() in msg for m in _TRANSIENT_MARKERS)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class CompletionResult:
    text: str                       # concatenated text blocks
    tool_calls: list[ToolCall] = field(default_factory=list)  # empty if no tools used
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""           # "end_turn" | "tool_use" | "max_tokens"
    raw_content: list[dict] = field(default_factory=list)     # canonical assistant blocks


# --------------------------------------------------------------------------- #
# Anthropic backend
# --------------------------------------------------------------------------- #
class _AnthropicBackend:
    def __init__(self, model: str, max_tokens: int):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def complete(self, system, messages, tools, temperature) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            resp = self._client.messages.create(**kwargs)
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
        except self._anthropic.APIError as exc:
            raise LLMError(f"Anthropic API call failed: {exc}") from exc

        text_parts, tool_calls, raw_content = [], [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
                raw_content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason or "",
            raw_content=raw_content,
        )


# --------------------------------------------------------------------------- #
# Google AI Studio (Gemini) backend
# --------------------------------------------------------------------------- #
_SCHEMA_DROP_KEYS = {"default", "$schema", "additionalProperties"}


def _clean_schema(schema: Any) -> Any:
    """Strip JSON-Schema keys the Gemini schema coercer rejects."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in _SCHEMA_DROP_KEYS}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


class _GeminiBackend:
    def __init__(self, model: str, max_tokens: int, thinking_budget: int | None = None):
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self.model = model
        self.max_tokens = max_tokens
        self.thinking_budget = thinking_budget

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LLMError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        self._client = genai.Client(api_key=api_key)

    # ----- canonical -> Gemini wire format --------------------------------

    def _build_tools(self, tools: list[dict]):
        declarations = [
            self._types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=_clean_schema(t["input_schema"]),
            )
            for t in tools
        ]
        return [self._types.Tool(function_declarations=declarations)]

    def _to_contents(self, messages: list[dict]):
        types = self._types
        # Map tool_use id -> function name so tool_result blocks can name their
        # function_response (Gemini pairs responses by name, not id).
        id_to_name: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        id_to_name[block["id"]] = block["name"]

        contents = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                gem_role = "user" if role == "user" else "model"
                contents.append(types.Content(role=gem_role, parts=[types.Part(text=content)]))
                continue

            parts = []
            gem_role = "model" if role == "assistant" else "user"
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    parts.append(types.Part(text=block["text"]))
                elif btype == "tool_use":
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=block["name"], args=block.get("input", {})
                            )
                        )
                    )
                elif btype == "tool_result":
                    name = id_to_name.get(block.get("tool_use_id"), "unknown_tool")
                    result = block.get("content", "")
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=name, response={"result": result}
                            )
                        )
                    )
            if parts:
                contents.append(types.Content(role=gem_role, parts=parts))
        return contents

    # ----- call -----------------------------------------------------------

    def complete(self, system, messages, tools, temperature) -> CompletionResult:
        types = self._types
        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": self.max_tokens,
        }
        if system:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = self._build_tools(tools)
        if self.thinking_budget is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.thinking_budget
            )

        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=self._to_contents(messages),
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:  # SDK raises a variety of error types
            raise LLMError(f"Gemini API call failed: {exc}") from exc

        text_parts, tool_calls, raw_content = [], [], []
        candidate = resp.candidates[0] if resp.candidates else None
        parts = (candidate.content.parts if candidate and candidate.content else []) or []
        for part in parts:
            if getattr(part, "text", None):
                text_parts.append(part.text)
                raw_content.append({"type": "text", "text": part.text})
            fc = getattr(part, "function_call", None)
            if fc is not None:
                call_id = f"call_{uuid.uuid4().hex[:12]}"
                args = dict(fc.args) if fc.args else {}
                tool_calls.append(ToolCall(id=call_id, name=fc.name, input=args))
                raw_content.append(
                    {"type": "tool_use", "id": call_id, "name": fc.name, "input": args}
                )

        # Derive a canonical stop_reason. Gemini reports STOP even when it emits
        # function calls, so treat any tool call as "tool_use".
        finish = str(getattr(candidate, "finish_reason", "") or "")
        if tool_calls:
            stop_reason = "tool_use"
        elif "MAX_TOKENS" in finish:
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        usage = getattr(resp, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            raw_content=raw_content,
        )


# --------------------------------------------------------------------------- #
# Public client
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(
        self,
        model: str,
        max_tokens: int,
        provider: str = "google",
        thinking_budget: int | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.provider = provider
        if provider == "google":
            self._backend = _GeminiBackend(model, max_tokens, thinking_budget)
        elif provider == "anthropic":
            self._backend = _AnthropicBackend(model, max_tokens)
        else:
            raise LLMError(f"unknown LLM provider: {provider!r} (expected 'google' or 'anthropic')")

    def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Call the backend, retrying transient failures with exponential backoff."""
        backoff = _TRANSIENT_BACKOFF_BASE
        for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
            try:
                return self._backend.complete(system, messages, tools, temperature)
            except LLMError as exc:
                if attempt < _MAX_TRANSIENT_RETRIES and _is_transient(exc):
                    short = str(exc).split("{", 1)[0].strip()[:80]
                    print(
                        f"[llm] transient error ({short}); retry "
                        f"{attempt + 1}/{_MAX_TRANSIENT_RETRIES} in {backoff:.0f}s",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise
