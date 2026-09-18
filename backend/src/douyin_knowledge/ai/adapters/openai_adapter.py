"""OpenAI adapter implementations.

Adapts OpenAI SDK to our provider protocols. Supports:
- ChatModel via GPT-4o, GPT-4o-mini
- StructuredModel via function calling
- VisionModel via GPT-4o vision
- ASRProvider via Whisper
- EmbeddingModel via text-embedding-3-small/large

Configuration:
- Set OPENAI_API_KEY environment variable
- Or pass api_key to constructors
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from douyin_knowledge.ai.providers import (
    ASRResponse,
    ChatMessage,
    ChatResponse,
    Embedding,
    StructuredResponse,
    TranscriptSegment,
    VisionResponse,
)
from douyin_knowledge.core.errors import ConfigurationError, DKError


class OpenAINotAvailable(DKError):
    """OpenAI SDK not installed."""
    pass


def _require_openai() -> None:
    if OpenAI is None:
        raise OpenAINotAvailable(
            "openai package not installed. Install with: pip install openai"
        )


class OpenAIChatModel:
    """ChatModel implementation using OpenAI."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Generate a response using OpenAI chat completion."""
        openai_messages = [{"role": m.role, "content": m.content} for m in messages]
        response = self._client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        return ChatResponse(
            content=choice.message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            } if response.usage else None,
            finish_reason=choice.finish_reason,
        )


class OpenAIStructuredModel:
    """StructuredModel implementation using OpenAI function calling."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> StructuredResponse:
        """Extract structured data using function calling."""
        function_def = {
            "name": "extract_data",
            "description": "Extract structured information from the text",
            "parameters": schema,
        }
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            functions=[function_def],
            function_call={"name": "extract_data"},
            temperature=temperature,
        )
        choice = response.choices[0]
        if choice.message.function_call:
            data = json.loads(choice.message.function_call.arguments)
        else:
            data = {}

        return StructuredResponse(
            data=data,
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            } if response.usage else None,
            raw_text=choice.message.content,
        )


class OpenAIVisionModel:
    """VisionModel implementation using GPT-4o vision."""

    def __init__(
        self,
        model: str = "gpt-4o",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def describe(
        self,
        image_path: str,
        prompt: str,
        *,
        detail: str = "auto",
    ) -> VisionResponse:
        """Describe an image using vision model."""
        import base64

        path = Path(image_path)
        if not path.exists():
            raise ConfigurationError(f"Image not found: {image_path}")

        with path.open("rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        # Infer MIME type from extension
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        mime_type = mime_map.get(path.suffix.lower(), "image/jpeg")

        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_data}",
                                "detail": detail,
                            },
                        },
                    ],
                }
            ],
        )
        choice = response.choices[0]
        return VisionResponse(
            description=choice.message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            } if response.usage else None,
        )


class OpenAIASRProvider:
    """ASRProvider implementation using Whisper."""

    def __init__(
        self,
        model: str = "whisper-1",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> ASRResponse:
        """Transcribe audio using Whisper."""
        path = Path(audio_path)
        if not path.exists():
            raise ConfigurationError(f"Audio file not found: {audio_path}")

        with path.open("rb") as f:
            response = self._client.audio.transcriptions.create(
                model=self.model,
                file=f,
                language=language,
                response_format="verbose_json",
                timestamp_granularities=[timestamp_granularity],
            )

        # Parse segments
        segments = []
        if hasattr(response, "segments") and response.segments:
            for seg in response.segments:
                segments.append(
                    TranscriptSegment(
                        text=seg.get("text", ""),
                        start_ms=int(seg.get("start", 0) * 1000),
                        end_ms=int(seg.get("end", 0) * 1000),
                    )
                )

        return ASRResponse(
            segments=segments,
            full_text=response.text,
            language=response.language if hasattr(response, "language") else None,
            model=self.model,
        )


class OpenAIEmbeddingModel:
    """EmbeddingModel implementation using OpenAI embeddings."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        _require_openai()
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def embed(self, text: str) -> Embedding:
        """Generate embedding for a single text."""
        response = self._client.embeddings.create(model=self.model, input=text)
        data = response.data[0]
        return Embedding(
            vector=data.embedding,
            model=response.model,
            dimensions=len(data.embedding),
        )

    def embed_batch(self, texts: list[str]) -> list[Embedding]:
        """Generate embeddings for multiple texts."""
        response = self._client.embeddings.create(model=self.model, input=texts)
        return [
            Embedding(
                vector=data.embedding,
                model=response.model,
                dimensions=len(data.embedding),
            )
            for data in response.data
        ]
