"""Tests for AI provider adapters."""

from __future__ import annotations

from douyin_knowledge.ai.adapters.mock_adapter import (
    MockASRProvider,
    MockChatModel,
    MockEmbeddingModel,
    MockOCRProvider,
    MockStructuredModel,
    MockVisionModel,
)
from douyin_knowledge.ai.providers import ChatMessage, TranscriptSegment


def test_mock_chat_model() -> None:
    model = MockChatModel(canned_response="Hello from mock")
    messages = [
        ChatMessage(role="system", content="You are helpful"),
        ChatMessage(role="user", content="Say hello"),
    ]
    response = model.generate(messages, temperature=0.5)
    assert response.content == "Hello from mock"
    assert response.model == "mock-chat"
    assert len(model.calls) == 1
    assert model.calls[0] == messages


def test_mock_structured_model() -> None:
    model = MockStructuredModel(canned_data={"name": "Test", "age": 25})
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    response = model.extract("Extract person info", schema)
    assert response.data == {"name": "Test", "age": 25}
    assert response.model == "mock-structured"
    assert len(model.calls) == 1
    assert model.calls[0][0] == "Extract person info"


def test_mock_vision_model() -> None:
    model = MockVisionModel(canned_description="A cat sitting on a table")
    response = model.describe("/path/to/image.jpg", "What is in this image?")
    assert response.description == "A cat sitting on a table"
    assert response.model == "mock-vision"
    assert len(model.calls) == 1
    assert model.calls[0] == ("/path/to/image.jpg", "What is in this image?")


def test_mock_asr_provider() -> None:
    segments = [
        TranscriptSegment(text="Hello", start_ms=0, end_ms=500),
        TranscriptSegment(text="world", start_ms=500, end_ms=1000),
    ]
    provider = MockASRProvider(canned_segments=segments)
    response = provider.transcribe("/path/to/audio.mp3", language="en")
    assert response.full_text == "Hello world"
    assert len(response.segments) == 2
    assert response.segments[0].text == "Hello"
    assert response.language == "en"
    assert len(provider.calls) == 1


def test_mock_ocr_provider() -> None:
    provider = MockOCRProvider(canned_text="Sample text from image")
    response = provider.extract_text("/path/to/image.jpg")
    assert response.full_text == "Sample text from image"
    assert len(response.results) == 1
    assert response.results[0].text == "Sample text from image"
    assert len(provider.calls) == 1


def test_mock_embedding_model() -> None:
    model = MockEmbeddingModel(dimensions=128)
    embedding = model.embed("test text")
    assert len(embedding.vector) == 128
    assert embedding.dimensions == 128
    assert embedding.model == "mock-embedding"
    assert len(model.calls) == 1


def test_mock_embedding_batch() -> None:
    model = MockEmbeddingModel(dimensions=64)
    embeddings = model.embed_batch(["text1", "text2", "text3"])
    assert len(embeddings) == 3
    assert all(len(e.vector) == 64 for e in embeddings)
    # One call per input. The previous version of this test asserted 6 because
    # embed_batch double-recorded; that was a bug in the adapter, not intended
    # behaviour — call counting is how tests assert cost, so inflating it hides
    # real duplicate-embedding regressions.
    assert model.calls == ["text1", "text2", "text3"]


def test_mock_embedding_is_not_degenerate() -> None:
    """Zero vectors make every cosine similarity 0.0, silently turning demo-mode
    semantic search into a no-op that still looks like it works."""
    model = MockEmbeddingModel(dimensions=256)
    related = model.embed("好运茶餐厅人均八十块很好吃")
    same_topic = model.embed("好运茶餐厅的菠萝油很推荐")
    unrelated = model.embed("Ruff 替代 flake8 的迁移配置")

    def cosine(a, b):
        # strict=True: two vectors of different length would silently truncate to the
        # shorter one, turning a dimension bug into a plausible-looking similarity score.
        return sum(x * y for x, y in zip(a.vector, b.vector, strict=True))

    assert any(v != 0.0 for v in related.vector)
    assert cosine(related, same_topic) > cosine(related, unrelated)
