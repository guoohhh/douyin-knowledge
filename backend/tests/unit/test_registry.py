"""Tests for capture provider registry."""

from __future__ import annotations

import pytest

from douyin_knowledge.ai.adapters.doubao_adapter import DoubaoASRProvider
from douyin_knowledge.ai.adapters.mock_adapter import MockASRProvider
from douyin_knowledge.ai.registry import get_asr_provider
from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider
from douyin_knowledge.capture.registry import get_capture_provider
from douyin_knowledge.config.settings import Settings
from douyin_knowledge.core.errors import ConfigurationError


def test_fixture_provider() -> None:
    settings = Settings(
        capture_provider="fixture",
        douyin_sidecar_identity="ignored-by-fixture",
    )
    provider = get_capture_provider(settings)
    assert isinstance(provider, FixtureCaptureProvider)
    assert provider.name == "fixture"


def test_douyin_provider_with_url() -> None:
    settings = Settings(
        capture_provider="douyin",
        douyin_sidecar_url="http://localhost:8000",
        douyin_sidecar_api_key="test-key",
        douyin_sidecar_identity="identity-123",
    )
    provider = get_capture_provider(settings)
    assert isinstance(provider, DouyinCaptureProvider)
    assert provider.name == "douyin"
    assert provider.base_url == "http://localhost:8000"
    assert provider.identity == "identity-123"


def test_douyin_provider_identity_is_optional() -> None:
    settings = Settings(
        capture_provider="douyin",
        douyin_sidecar_url="http://localhost:8000",
    )
    provider = get_capture_provider(settings)
    assert isinstance(provider, DouyinCaptureProvider)
    assert provider.identity is None


def test_douyin_identity_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_DOUYIN_SIDECAR_IDENTITY", "identity-from-env")
    settings = Settings(_env_file=None)
    assert settings.douyin_sidecar_identity == "identity-from-env"


def test_douyin_provider_missing_url() -> None:
    settings = Settings(capture_provider="douyin", douyin_sidecar_url="")
    with pytest.raises(ConfigurationError, match="requires DK_DOUYIN_SIDECAR_URL"):
        get_capture_provider(settings)


def test_doubao_asr_provider_from_settings() -> None:
    settings = Settings(
        ai_provider="mock",
        asr_provider="doubao",
        doubao_asr_api_key="not-a-real-key",
        doubao_asr_resource_id="volc.seedasr.sauc.duration",
    )
    provider = get_asr_provider(settings)
    assert isinstance(provider, DoubaoASRProvider)
    assert provider.resource_id == "volc.seedasr.sauc.duration"
    assert settings.model_for_role("asr") == "doubao-seed-asr-2.0"
    assert settings.missing_provider_credentials() == []


def test_doubao_asr_key_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_ASR_PROVIDER", "doubao")
    monkeypatch.setenv("DK_DOUBAO_ASR_API_KEY", "key-from-env")
    settings = Settings(_env_file=None)
    assert settings.asr_provider == "doubao"
    assert settings.doubao_asr_api_key == "key-from-env"
    assert settings.redacted()["doubao_asr_api_key"] is True
    assert "key-from-env" not in repr(settings)


def test_doubao_asr_requires_provider_specific_key() -> None:
    settings = Settings(ai_provider="mock", asr_provider="doubao")
    assert settings.missing_provider_credentials() == ["DK_DOUBAO_ASR_API_KEY"]
    with pytest.raises(ConfigurationError, match="DK_DOUBAO_ASR_API_KEY"):
        get_asr_provider(settings)


def test_mock_asr_registry_is_unchanged() -> None:
    assert isinstance(get_asr_provider(Settings(ai_provider="mock")), MockASRProvider)
