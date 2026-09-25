"""AI provider registry and factory.

Central configuration point for all AI capabilities. Selects concrete implementations
based on settings (e.g., DK_AI_PROVIDER=openai vs mock).

Design:
- Domain code depends on protocols (ai.providers), not adapters
- This registry resolves Settings → concrete adapter instances
- Supports hot-swapping providers for testing, cost optimization, or offline use
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from douyin_knowledge.ai.providers import (
    ASRProvider,
    ChatModel,
    EmbeddingModel,
    OCRProvider,
    StructuredModel,
    VisionModel,
)
from douyin_knowledge.core.errors import ConfigurationError

if TYPE_CHECKING:
    from douyin_knowledge.config.settings import Settings


def get_chat_model(settings: Settings) -> ChatModel:
    """Build the configured chat model."""
    provider = settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockChatModel
        return MockChatModel()
    if provider == "openai":
        from douyin_knowledge.ai.adapters.openai_adapter import OpenAIChatModel
        return OpenAIChatModel(
            model=settings.openai_chat_model or "gpt-4o-mini",
            api_key=settings.openai_api_key,
        )
    raise ConfigurationError(f"unknown ai_provider {provider!r}")


def get_answer_chat_model(settings: Settings) -> ChatModel | None:
    """Build the chat model used to *phrase* an answer, or `None` to compose it directly.

    Returns `None` under `ai_provider=mock` rather than `MockChatModel`, and the difference
    is user-visible. `MockChatModel` echoes a canned string, so the demo path was serving
    the literal body "Mock response" with real evidence and a real citation marker attached
    -- a citation supporting a sentence that asserts nothing. `AnswerGenerator` treats a
    missing model as "compose the answer from the retrieved evidence yourself"
    (`_deterministic_answer`), which is the honest behaviour for a mode whose whole purpose
    is to exercise the cited-answer path before anyone configures a key (DEC-006).

    Distinct from `get_chat_model` because the two callers want different things: an answer
    generator can degrade to deterministic prose, whereas a caller that needs generation
    unconditionally should keep getting a model (and the mock, in tests).
    """
    if settings.ai_provider == "mock":
        return None
    return get_chat_model(settings)


def get_structured_model(settings: Settings) -> StructuredModel:
    """Build the configured structured extraction model."""
    provider = settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockStructuredModel
        return MockStructuredModel()
    if provider == "openai":
        from douyin_knowledge.ai.adapters.openai_adapter import OpenAIStructuredModel
        return OpenAIStructuredModel(
            model=settings.openai_chat_model or "gpt-4o-mini",
            api_key=settings.openai_api_key,
        )
    raise ConfigurationError(f"unknown ai_provider {provider!r}")


def get_vision_model(settings: Settings) -> VisionModel:
    """Build the configured vision model."""
    provider = settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockVisionModel
        return MockVisionModel()
    if provider == "openai":
        from douyin_knowledge.ai.adapters.openai_adapter import OpenAIVisionModel
        return OpenAIVisionModel(
            model=settings.openai_vision_model or "gpt-4o",
            api_key=settings.openai_api_key,
        )
    raise ConfigurationError(f"unknown ai_provider {provider!r}")


def get_asr_provider(settings: Settings) -> ASRProvider:
    """Build the configured ASR provider."""
    provider = settings.asr_provider or settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockASRProvider
        return MockASRProvider()
    if provider == "openai":
        from douyin_knowledge.ai.adapters.openai_adapter import OpenAIASRProvider
        return OpenAIASRProvider(api_key=settings.openai_api_key)
    if provider == "doubao":
        from douyin_knowledge.ai.adapters.doubao_adapter import DoubaoASRProvider
        if not settings.doubao_asr_api_key:
            raise ConfigurationError("asr_provider=doubao requires DK_DOUBAO_ASR_API_KEY")
        return DoubaoASRProvider(
            api_key=settings.doubao_asr_api_key,
            resource_id=settings.doubao_asr_resource_id,
            endpoint=settings.doubao_asr_endpoint,
            timeout_s=settings.doubao_asr_timeout_s,
        )
    raise ConfigurationError(f"unknown asr_provider {provider!r}")


def get_ocr_provider(settings: Settings) -> OCRProvider:
    """Build the configured OCR provider."""
    provider = settings.ocr_provider or settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockOCRProvider
        return MockOCRProvider()
    if provider == "openai":
        # OpenAI doesn't have a dedicated OCR API; use vision model with OCR prompt
        raise ConfigurationError("ocr_provider=openai not yet implemented; use mock or add OCR adapter")
    raise ConfigurationError(f"unknown ocr_provider {provider!r}")


def get_embedding_model(settings: Settings) -> EmbeddingModel:
    """Build the configured embedding model."""
    provider = settings.embedding_provider or settings.ai_provider
    if provider == "mock":
        from douyin_knowledge.ai.adapters.mock_adapter import MockEmbeddingModel
        return MockEmbeddingModel()
    if provider == "openai":
        from douyin_knowledge.ai.adapters.openai_adapter import OpenAIEmbeddingModel
        return OpenAIEmbeddingModel(
            model=settings.openai_embedding_model or "text-embedding-3-small",
            api_key=settings.openai_api_key,
        )
    raise ConfigurationError(f"unknown embedding_provider {provider!r}")
