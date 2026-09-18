"""AI provider interfaces and adapters.

Capability-based abstractions over LLM, ASR, OCR, and embedding providers.
Domain code depends on these interfaces, not vendor SDKs directly.
"""

from douyin_knowledge.ai.providers import (
    ASRProvider,
    ChatModel,
    EmbeddingModel,
    OCRProvider,
    StructuredModel,
    VisionModel,
)

__all__ = [
    "ASRProvider",
    "ChatModel",
    "EmbeddingModel",
    "OCRProvider",
    "StructuredModel",
    "VisionModel",
]
