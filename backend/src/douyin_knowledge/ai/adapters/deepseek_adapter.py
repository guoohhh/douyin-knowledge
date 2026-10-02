"""DeepSeek chat and structured-output adapters.

DeepSeek exposes an OpenAI-compatible transport, but it remains a distinct provider:
credentials, defaults, provenance, and non-thinking controls are all DeepSeek-specific.
Structured extraction uses the current ``tools`` / ``tool_choice`` / ``tool_calls``
contract instead of the legacy ``functions`` API.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as validate_json_schema

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from douyin_knowledge.ai.providers import ChatMessage, ChatResponse, StructuredResponse
from douyin_knowledge.core.errors import DKError, StructuredOutputInvalid


class DeepSeekNotAvailable(DKError):
    """The OpenAI-compatible SDK transport is not installed."""


def _require_openai() -> None:
    if OpenAI is None:
        raise DeepSeekNotAvailable(
            "openai package not installed. Install with: pip install 'douyin-knowledge[deepseek]'"
        )


def _usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    result: dict[str, int] = {}
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value

    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None)
    if isinstance(reasoning_tokens, int):
        result["reasoning_tokens"] = reasoning_tokens
    return result or None


class DeepSeekChatModel:
    """Chat generation through DeepSeek's OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str = "deepseek-flash",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout_s: float = 120.0,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": message.role, "content": message.content} for message in messages],
            temperature=temperature,
            max_tokens=max_tokens,
            # DeepSeek enables thinking by default. Stage 3C deliberately validates
            # deterministic non-thinking extraction/chat behaviour.
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = response.choices[0]
        return ChatResponse(
            content=choice.message.content or "",
            model=response.model,
            usage=_usage(response),
            finish_reason=choice.finish_reason,
        )


class DeepSeekStructuredModel:
    """Schema-checked structured extraction using modern DeepSeek tool calls."""

    _TOOL_NAME = "extract_data"

    def __init__(
        self,
        model: str = "deepseek-flash",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout_s: float = 120.0,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)

    def extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> StructuredResponse:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": self._TOOL_NAME,
                        "description": "Return the requested structured extraction",
                        "parameters": schema,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": self._TOOL_NAME}},
            temperature=temperature,
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = response.choices[0]
        if choice.finish_reason in {
            "length",
            "content_filter",
            "insufficient_system_resource",
            "aborted",
        }:
            raise StructuredOutputInvalid(
                "model response ended before structured extraction completed",
                model=response.model,
                finish_reason=choice.finish_reason,
            )

        tool_calls = choice.message.tool_calls or []
        matching = [
            call
            for call in tool_calls
            if getattr(getattr(call, "function", None), "name", None) == self._TOOL_NAME
        ]
        if len(matching) != 1:
            raise StructuredOutputInvalid(
                "model did not return exactly one forced extract_data tool call",
                model=response.model,
                finish_reason=choice.finish_reason,
                matching_tool_calls=len(matching),
            )

        raw_arguments = matching[0].function.arguments
        try:
            data = json.loads(raw_arguments)
        except (TypeError, ValueError) as exc:
            raise StructuredOutputInvalid(
                f"model returned unparseable tool arguments: {exc}",
                model=response.model,
            ) from exc
        if not isinstance(data, dict):
            raise StructuredOutputInvalid(
                f"expected a JSON object from the model, got {type(data).__name__}",
                model=response.model,
            )

        try:
            validate_json_schema(instance=data, schema=schema)
        except JSONSchemaValidationError as exc:
            raise StructuredOutputInvalid(
                f"model output did not satisfy the requested schema: {exc.message}",
                model=response.model,
                schema_path=list(exc.schema_path),
                instance_path=list(exc.path),
            ) from exc

        return StructuredResponse(
            data=data,
            model=response.model,
            usage=_usage(response),
            raw_text=choice.message.content,
        )


__all__ = ["DeepSeekChatModel", "DeepSeekStructuredModel"]
