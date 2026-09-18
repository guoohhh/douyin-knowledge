"""Mock AI providers for testing.

Simple in-memory implementations that return canned responses without making
external API calls. Used for testing domain logic independently of AI vendors.
"""

from __future__ import annotations

import hashlib
import math

from douyin_knowledge.ai.providers import (
    ASRResponse,
    ChatMessage,
    ChatResponse,
    Embedding,
    OCRResponse,
    OCRResult,
    StructuredResponse,
    TranscriptSegment,
    VisionResponse,
)


class MockChatModel:
    """Mock chat model that echoes prompts."""

    def __init__(self, canned_response: str = "Mock response") -> None:
        self.canned_response = canned_response
        self.calls: list[list[ChatMessage]] = []

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(
            content=self.canned_response,
            model="mock-chat",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


class MockStructuredModel:
    """Offline structured extraction backed by rules instead of a model.

    Returning empty data here would make demo mode — the no-API-key path a new
    user hits first — build a knowledge base with no knowledge in it, and every
    layer downstream would look broken for reasons unrelated to itself. So this
    fulfils the same contract using the deterministic heuristics in
    `extraction.heuristics`, dispatching on the requested schema.

    Pass `canned_data` to override entirely, which is what tests do when they
    need one exact payload.
    """

    def __init__(self, canned_data: dict | None = None) -> None:
        self.canned_data = canned_data
        self.calls: list[tuple[str, dict]] = []

    def extract(
        self,
        prompt: str,
        schema: dict,
        *,
        temperature: float = 0.0,
    ) -> StructuredResponse:
        self.calls.append((prompt, schema))
        data = self.canned_data if self.canned_data is not None else self._derive(prompt, schema)
        return StructuredResponse(
            data=data,
            model="mock-structured",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    @staticmethod
    def _derive(prompt: str, schema: dict) -> dict:
        # Imported lazily: ai/ must not import extraction/ at module scope, or
        # the dependency runs backwards (extraction depends on ai).
        from douyin_knowledge.extraction.heuristics import extract_claims, extract_mentions

        # The prompt embeds the text after a marker; fall back to the whole
        # prompt when the caller used a different shape.
        text = prompt.split("<<<TEXT>>>")[-1].strip() if "<<<TEXT>>>" in prompt else prompt
        properties = schema.get("properties", {})

        if "mentions" in properties:
            return {
                "mentions": [
                    {
                        "text": m.text,
                        "entity_type": m.entity_type,
                        "context": m.context,
                    }
                    for m in extract_mentions(text)
                ]
            }
        if "claims" in properties:
            return {"claims": [c.as_dict() for c in extract_claims(text)]}
        return {}


class MockVisionModel:
    """Mock vision model that returns canned descriptions."""

    def __init__(self, canned_description: str = "Mock image description") -> None:
        self.canned_description = canned_description
        self.calls: list[tuple[str, str]] = []

    def describe(
        self,
        image_path: str,
        prompt: str,
        *,
        detail: str = "auto",
    ) -> VisionResponse:
        self.calls.append((image_path, prompt))
        return VisionResponse(
            description=self.canned_description,
            model="mock-vision",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


class MockASRProvider:
    """Mock ASR that returns preset transcript."""

    def __init__(self, canned_segments: list[TranscriptSegment] | None = None) -> None:
        self.canned_segments = canned_segments or [
            TranscriptSegment(text="Mock transcript", start_ms=0, end_ms=1000)
        ]
        self.calls: list[str] = []

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> ASRResponse:
        self.calls.append(audio_path)
        return ASRResponse(
            segments=self.canned_segments,
            full_text=" ".join(seg.text for seg in self.canned_segments),
            language=language or "zh",
            model="mock-asr",
        )


class MockOCRProvider:
    """Mock OCR that returns preset text."""

    def __init__(self, canned_text: str = "Mock OCR text") -> None:
        self.canned_text = canned_text
        self.calls: list[str] = []

    def extract_text(
        self,
        image_path: str,
        *,
        language: str | None = None,
    ) -> OCRResponse:
        self.calls.append(image_path)
        return OCRResponse(
            results=[OCRResult(text=self.canned_text, confidence=0.95)],
            full_text=self.canned_text,
            model="mock-ocr",
        )


class MockEmbeddingModel:
    """Deterministic offline embedding model.

    Zero vectors would make every cosine similarity 0.0, which silently turns
    semantic search into a no-op in demo mode. Instead this hashes character
    n-grams into a fixed-width bag-of-features and L2-normalizes the result, so:

    * the same text always embeds to the same vector (reproducible tests);
    * texts sharing n-grams score higher than unrelated ones (search is usable);
    * no network or model download is required.

    It is a real (if crude) lexical embedding, not a placeholder.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.calls: list[str] = []

    def _features(self, text: str) -> list[str]:
        cleaned = " ".join(text.split()).casefold()
        grams = [cleaned[i : i + 2] for i in range(max(0, len(cleaned) - 1))]
        grams.extend(cleaned.split())
        return grams or [cleaned]

    def embed(self, text: str) -> Embedding:
        self.calls.append(text)
        vector = [0.0] * self.dimensions
        for gram in self._features(text):
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return Embedding(
            vector=vector,
            model="mock-embedding",
            dimensions=self.dimensions,
        )

    def embed_batch(self, texts: list[str]) -> list[Embedding]:
        return [self.embed(text) for text in texts]
