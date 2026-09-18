"""Provider protocol definitions for AI capabilities.

These protocols define the contract between Douyin Knowledge's domain logic and
the underlying AI services. Each protocol represents one capability dimension:

- ChatModel: conversational text generation
- StructuredModel: structured output extraction (JSON with schema)
- VisionModel: image understanding
- ASRProvider: audio transcription
- OCRProvider: text extraction from images
- EmbeddingModel: vector embeddings for semantic search

Implementations live in adapters/ subdirectory. Domain code depends only on these
protocols, never on vendor SDKs (OpenAI, Anthropic, etc.) directly.

Design principles (from AI_PIPELINE.md):
1. Progressive processing: start cheap, escalate only when needed
2. Evidence-first: every interpretation should be traceable to evidence
3. Confidence-aware: models should admit uncertainty
4. Vendor-neutral: swap providers without changing domain logic
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ChatMessage:
    """Single message in a conversation."""
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ChatResponse:
    """Response from a chat model."""
    content: str
    model: str
    usage: dict[str, int] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class StructuredResponse:
    """Structured extraction result."""
    data: dict[str, Any]
    model: str
    usage: dict[str, int] | None = None
    raw_text: str | None = None


@dataclass(frozen=True)
class VisionResponse:
    """Vision understanding result."""
    description: str
    model: str
    usage: dict[str, int] | None = None
    structured_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    """One segment of transcribed speech."""
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker_id: str | None = None


@dataclass(frozen=True)
class ASRResponse:
    """Audio transcription result."""
    segments: list[TranscriptSegment]
    full_text: str
    language: str | None = None
    model: str = "unknown"


@dataclass(frozen=True)
class OCRResult:
    """Text extracted from an image."""
    text: str
    confidence: float | None = None
    bounding_box: dict[str, int] | None = None


@dataclass(frozen=True)
class OCRResponse:
    """OCR result for one image."""
    results: list[OCRResult]
    full_text: str
    model: str = "unknown"


@dataclass(frozen=True)
class Embedding:
    """Vector embedding result."""
    vector: list[float]
    model: str
    dimensions: int


class ChatModel(Protocol):
    """Protocol for conversational text generation."""

    @abstractmethod
    def generate(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Generate a response to a conversation."""
        ...


class StructuredModel(Protocol):
    """Protocol for structured output extraction.

    Implementations should use function calling, JSON mode, or guided generation
    to ensure schema conformance.
    """

    @abstractmethod
    def extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
    ) -> StructuredResponse:
        """Extract structured data according to a JSON schema."""
        ...


class VisionModel(Protocol):
    """Protocol for image understanding."""

    @abstractmethod
    def describe(
        self,
        image_path: str,
        prompt: str,
        *,
        detail: str = "auto",
    ) -> VisionResponse:
        """Generate a description of an image given a prompt."""
        ...


class ASRProvider(Protocol):
    """Protocol for automatic speech recognition."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> ASRResponse:
        """Transcribe audio to text with timestamps."""
        ...


class OCRProvider(Protocol):
    """Protocol for optical character recognition."""

    @abstractmethod
    def extract_text(
        self,
        image_path: str,
        *,
        language: str | None = None,
    ) -> OCRResponse:
        """Extract text from an image."""
        ...


class EmbeddingModel(Protocol):
    """Protocol for vector embeddings."""

    @abstractmethod
    def embed(
        self,
        text: str,
    ) -> Embedding:
        """Generate a vector embedding for text."""
        ...

    @abstractmethod
    def embed_batch(
        self,
        texts: list[str],
    ) -> list[Embedding]:
        """Generate embeddings for multiple texts."""
        ...


class AIProvider(Protocol):
    """Unified AI provider interface for extraction tasks.

    This is a convenience protocol that combines common AI capabilities
    needed by extraction pipelines. Implementations can delegate to
    specialized providers internally.
    """

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """Generate text from a prompt.

        Args:
            prompt: Input prompt
            model: Model identifier
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        ...

    @abstractmethod
    def get_embedding_model(self) -> EmbeddingModel:
        """Get the embedding model for this provider.

        Returns:
            EmbeddingModel instance
        """
        ...


def get_default_provider() -> AIProvider:
    """Get the default AI provider instance.

    Returns:
        Configured AIProvider (currently uses OpenAI adapter)
    """
    from douyin_knowledge.ai.adapters.openai_adapter import OpenAIAdapter
    return OpenAIAdapter()
