"""AI settings are local, explicit, redacted, and take effect after restart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from douyin_knowledge.api import create_app
from douyin_knowledge.config.ai_local import (
    AIConfigUpdate,
    config_view,
    save_local,
    validate_processing_configuration,
)
from douyin_knowledge.config.settings import Settings, get_settings, reset_settings_cache
from douyin_knowledge.core.errors import ConfigurationError

MOCK_MODELS = {
    "triage": "mock-triage", "extraction": "mock-extraction", "answer": "mock-answer",
    "embedding": "mock-embedding", "asr": "mock-asr",
}


def test_private_storage_redaction_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_DATA_DIR", str(tmp_path))
    reset_settings_cache()
    original = get_settings()
    update = AIConfigUpdate(
        ai_provider="deepseek", asr_provider="mock", embedding_provider="mock",
        models={**MOCK_MODELS, "triage": "deepseek-flash", "extraction": "deepseek-flash", "answer": "deepseek-flash"},
        credentials={"deepseek": "private-test-key"},
    )
    save_local(original, update)
    assert "private-test-key" not in (tmp_path / "ai-settings.json").read_text()
    assert (tmp_path / "ai-secrets.json").stat().st_mode & 0o077 == 0
    assert "private-test-key" not in json.dumps(config_view(original))
    assert config_view(original)["restart_required"] is True
    assert original.ai_provider == "mock"

    reset_settings_cache()
    active = get_settings()
    assert active.ai_provider == "deepseek"
    assert active.deepseek_api_key == "private-test-key"
    assert active.model_for_role("extraction") == "deepseek-flash"
    assert config_view(active)["restart_required"] is False
    reset_settings_cache()


def test_environment_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_DATA_DIR", str(tmp_path))
    save_local(Settings(data_dir=tmp_path), AIConfigUpdate(
        ai_provider="deepseek", asr_provider="mock", embedding_provider="mock",
        models={**MOCK_MODELS, "triage": "deepseek-flash", "extraction": "deepseek-flash", "answer": "deepseek-flash"},
        credentials={"deepseek": "file-secret"},
    ))
    monkeypatch.setenv("DK_AI_PROVIDER", "mock")
    monkeypatch.setenv("DK_DEEPSEEK_API_KEY", "env-secret")
    reset_settings_cache()
    active = get_settings()
    assert active.ai_provider == "mock"
    assert active.deepseek_api_key == "env-secret"
    reset_settings_cache()


def test_unsupported_model_and_provider_role_fail_before_processing() -> None:
    with pytest.raises(PydanticValidationError, match="not supported"):
        AIConfigUpdate(
            ai_provider="deepseek", asr_provider="mock", embedding_provider="mock",
            models={**MOCK_MODELS, "extraction": "made-up-model"},
        )
    with pytest.raises(ConfigurationError, match="does not support embedding"):
        validate_processing_configuration(Settings(ai_provider="deepseek"), 1)
    with pytest.raises(ConfigurationError, match="DK_DEEPSEEK_API_KEY"):
        validate_processing_configuration(
            Settings(ai_provider="deepseek", asr_provider="mock", embedding_provider="mock"), 2
        )
    validate_processing_configuration(Settings(ai_provider="mock"), 2)


def test_legacy_deepseek_defaults_offer_supported_choices(tmp_path: Path) -> None:
    view = config_view(Settings(data_dir=tmp_path, ai_provider="deepseek"))
    assert view["effective"]["asr_provider"] == "deepseek"
    assert view["saved"]["asr_provider"] == "mock"
    assert view["saved"]["embedding_provider"] == "mock"
    assert view["restart_required"] is False


def test_saving_equivalent_mock_defaults_does_not_claim_restart(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    save_local(settings, AIConfigUpdate(
        ai_provider="mock", asr_provider="mock", embedding_provider="mock", models=MOCK_MODELS,
    ))
    assert config_view(settings)["restart_required"] is False


def test_api_saves_and_never_returns_secret(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, serve_frontend=False)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        response = client.put("/api/admin/ai-configuration", json={
            "ai_provider": "deepseek", "asr_provider": "mock", "embedding_provider": "mock",
            "models": {**MOCK_MODELS, "triage": "deepseek-flash", "extraction": "deepseek-flash", "answer": "deepseek-flash"},
            "credentials": {"deepseek": "private-test-key"},
        })
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] is True
        assert response.json()["credentials_configured"]["deepseek"] is True
        assert "private-test-key" not in response.text
        assert "private-test-key" not in client.get("/api/admin/ai-configuration").text
        invalid = client.put("/api/admin/ai-configuration", json={
            "ai_provider": "deepseek", "asr_provider": "mock", "embedding_provider": "mock",
            "models": {**MOCK_MODELS, "triage": "deepseek-flash", "extraction": "unsupported", "answer": "deepseek-flash"},
            "credentials": {"deepseek": "never-echo-this-key"},
        })
        assert invalid.status_code == 422
        assert "never-echo-this-key" not in invalid.text


def test_secret_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "ai-secrets.json"
    path.write_text('{"deepseek_api_key": "private-test-key"}')
    path.chmod(0o644)
    with pytest.raises(ConfigurationError, match="readable only by its owner"):
        config_view(Settings(data_dir=tmp_path))
