"""Local AI choices and credentials, loaded at process start.

Choices and secrets deliberately live in separate files under the private data
directory. Environment variables (including .env) take precedence. Neither file is
part of the SQLite product database or the repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.core.errors import ConfigurationError
from douyin_knowledge.db.models.search import VectorDocument

if TYPE_CHECKING:
    from douyin_knowledge.config.settings import Settings

CHOICE_FILE = "ai-settings.json"
SECRET_FILE = "ai-secrets.json"

CHOICE_FIELDS = frozenset(
    {"ai_provider", "asr_provider", "embedding_provider", "ocr_provider",
     "triage_model", "extraction_model", "answer_model", "embedding_model", "asr_model"}
)
SECRET_FIELDS = frozenset({"openai_api_key", "deepseek_api_key", "doubao_asr_api_key"})

# These are the models already configured in this repository's adapters. A new
# provider/model needs an adapter contract test before it appears in the product UI.
MODEL_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "triage": {"mock": ("mock-triage",), "openai": ("gpt-4o-mini", "gpt-4o"), "deepseek": ("deepseek-flash",)},
    "extraction": {"mock": ("mock-extraction",), "openai": ("gpt-4o-mini", "gpt-4o"), "deepseek": ("deepseek-flash",)},
    "answer": {"mock": ("mock-answer",), "openai": ("gpt-4o-mini", "gpt-4o"), "deepseek": ("deepseek-flash",)},
    "embedding": {"mock": ("mock-embedding",), "openai": ("text-embedding-3-small",)},
    "asr": {"mock": ("mock-asr",), "openai": ("whisper-1",), "doubao": ("doubao-seed-asr-2.0",)},
}


class AIConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_provider: Literal["mock", "openai", "deepseek"]
    asr_provider: Literal["mock", "openai", "doubao"]
    embedding_provider: Literal["mock", "openai"]
    models: dict[Literal["triage", "extraction", "answer", "embedding", "asr"], str]
    credentials: dict[Literal["openai", "deepseek", "doubao"], str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_supported(self) -> AIConfigUpdate:
        if set(self.models) != set(MODEL_OPTIONS):
            raise ValueError("models must specify triage, extraction, answer, embedding, and asr")
        providers = {
            "triage": self.ai_provider,
            "extraction": self.ai_provider,
            "answer": self.ai_provider,
            "embedding": self.embedding_provider,
            "asr": self.asr_provider,
        }
        for role, model in self.models.items():
            if model not in MODEL_OPTIONS[role][providers[role]]:
                raise ValueError(f"{model!r} is not supported for {providers[role]} {role}")
        for provider, value in self.credentials.items():
            if not value.strip():
                raise ValueError(f"{provider} credential cannot be empty")
        return self


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"invalid local AI configuration file: {path.name}")
    if path.name == SECRET_FILE and path.stat().st_mode & 0o077:
        raise ConfigurationError("local AI credentials file must be readable only by its owner")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read local AI configuration: {path.name}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"invalid local AI configuration: {path.name}")
    return data


def read_local(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = data_dir.resolve()
    choices = _read(root / CHOICE_FILE)
    secrets = _read(root / SECRET_FILE)
    if choices.keys() - CHOICE_FIELDS or secrets.keys() - SECRET_FIELDS:
        raise ConfigurationError("local AI configuration contains unsupported fields")
    return choices, secrets


def local_overrides(data_dir: Path) -> dict[str, Any]:
    """Return only values without a DK_* environment or .env override."""
    choices, secrets = read_local(data_dir)
    dotenv = dotenv_values(".env")
    overrides = {
        key: value
        for key, value in (choices | secrets).items()
        if f"DK_{key.upper()}" not in os.environ
        and f"DK_{key.upper()}" not in dotenv
    }
    # Provider overrides also invalidate the saved model choices for that
    # provider. Otherwise DK_AI_PROVIDER=mock could inherit a DeepSeek model
    # label and write false provenance for a mock processing run.
    explicit = set(os.environ) | set(dotenv)
    if "DK_AI_PROVIDER" in explicit:
        for role in ("triage", "extraction", "answer"):
            overrides.pop(f"{role}_model", None)
    if "DK_ASR_PROVIDER" in explicit:
        overrides.pop("asr_model", None)
    if "DK_EMBEDDING_PROVIDER" in explicit:
        overrides.pop("embedding_model", None)
    return overrides


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ConfigurationError(f"refusing to replace symlink: {path.name}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_local(settings: Settings, update: AIConfigUpdate) -> None:
    choices, secrets = read_local(settings.data_dir)
    choices.update(
        ai_provider=update.ai_provider,
        asr_provider=update.asr_provider,
        embedding_provider=update.embedding_provider,
        ocr_provider="mock",  # no real OCR adapter is wired into processing
    )
    for role, model in update.models.items():
        choices[f"{role}_model"] = model
    for provider, value in update.credentials.items():
        secrets[f"{provider}_api_key" if provider != "doubao" else "doubao_asr_api_key"] = value.strip()
    root = settings.data_dir.resolve()
    # Write secrets first. An interrupted save can leave unused new credentials,
    # but cannot activate new provider choices without the matching credentials.
    if update.credentials:
        _atomic_write(root / SECRET_FILE, secrets)
    _atomic_write(root / CHOICE_FILE, choices)


def config_view(settings: Settings) -> dict[str, Any]:
    choices, secrets = read_local(settings.data_dir)
    saved_provider = str(choices.get("ai_provider", settings.ai_provider))
    saved_asr = str(choices.get("asr_provider", settings.asr_provider or settings.ai_provider))
    saved_embedding = str(choices.get("embedding_provider", settings.embedding_provider or settings.ai_provider))
    if saved_asr not in MODEL_OPTIONS["asr"]:
        saved_asr = "mock"
    if saved_embedding not in MODEL_OPTIONS["embedding"]:
        saved_embedding = "mock"
    providers = {"triage": saved_provider, "extraction": saved_provider,
                 "answer": saved_provider, "asr": saved_asr, "embedding": saved_embedding}
    saved_models = {
        role: str(choices.get(f"{role}_model") or (
            settings.model_for_role(role) if provider == settings.provider_for_role(role)
            else MODEL_OPTIONS[role][provider][0]
        ))
        for role, provider in providers.items()
    }
    current = {role: settings.model_for_role(role) for role in providers}
    effective_providers = {
        "ai_provider": settings.ai_provider,
        "asr_provider": settings.asr_provider or settings.ai_provider,
        "embedding_provider": settings.embedding_provider or settings.ai_provider,
    }
    saved_providers = {
        "ai_provider": saved_provider,
        "asr_provider": saved_asr,
        "embedding_provider": saved_embedding,
    }
    explicit = set(os.environ) | set(dotenv_values(".env"))
    provider_changed = any(
        key in choices and value != effective_providers[key] and f"DK_{key.upper()}" not in explicit
        for key, value in saved_providers.items()
    )
    models_changed = any(
        f"{role}_model" in choices and saved_models[role] != current[role]
        and f"DK_{role.upper()}_MODEL" not in explicit
        and f"DK_{'ASR' if role == 'asr' else 'EMBEDDING' if role == 'embedding' else 'AI'}_PROVIDER" not in explicit
        for role in providers
    )
    secrets_changed = any(
        key in secrets and getattr(settings, key) != value
        and f"DK_{key.upper()}" not in explicit
        for key, value in secrets.items()
    )
    pending = provider_changed or models_changed or secrets_changed
    credential_status = {}
    for provider, field in (("openai", "openai_api_key"),
                            ("deepseek", "deepseek_api_key"),
                            ("doubao", "doubao_asr_api_key")):
        credential_status[provider] = bool(
            getattr(settings, field) if f"DK_{field.upper()}" in explicit
            else secrets.get(field) or getattr(settings, field)
        )
    return {
        "effective": {
            "ai_provider": settings.ai_provider,
            "asr_provider": settings.asr_provider or settings.ai_provider,
            "embedding_provider": settings.embedding_provider or settings.ai_provider,
            "models": current,
        },
        "saved": {
            "ai_provider": saved_provider,
            "asr_provider": saved_asr,
            "embedding_provider": saved_embedding,
            "models": saved_models,
        },
        "credentials_configured": credential_status,
        "restart_required": pending,
        "sdk_available": {"openai": importlib.util.find_spec("openai") is not None,
                          "deepseek": importlib.util.find_spec("openai") is not None,
                          "doubao": True},
        "model_options": MODEL_OPTIONS,
    }


def validate_processing_configuration(settings: Settings, target_level: int) -> None:
    """Fail before a paid job is accepted with an unusable selected provider."""
    active = {
        "extraction": settings.ai_provider,
        "embedding": settings.embedding_provider or settings.ai_provider,
    }
    if target_level >= 2:
        active["asr"] = settings.asr_provider or settings.ai_provider
    supported = {"extraction": {"mock", "openai", "deepseek"},
                 "embedding": {"mock", "openai"},
                 "asr": {"mock", "openai", "doubao"}}
    for role, provider in active.items():
        if provider not in supported[role]:
            raise ConfigurationError(f"{provider} does not support {role}; choose a supported provider in Settings")
    keys = {"openai": settings.openai_api_key,
            "deepseek": settings.deepseek_api_key,
            "doubao": settings.doubao_asr_api_key}
    env_names = {"openai": "DK_OPENAI_API_KEY", "deepseek": "DK_DEEPSEEK_API_KEY",
                 "doubao": "DK_DOUBAO_ASR_API_KEY"}
    for provider in set(active.values()) - {"mock"}:
        if not keys[provider]:
            raise ConfigurationError(f"{provider} requires {env_names[provider]} or a saved local credential")
        if provider in {"openai", "deepseek"} and importlib.util.find_spec("openai") is None:
            raise ConfigurationError(f"{provider} adapter requires: pip install 'douyin-knowledge[{provider}]'")


def validate_embedding_index_configuration(session: Session, settings: Settings) -> None:
    """Avoid a paid whole-corpus embed followed by a mixed-dimension index failure.

    The current vector store holds one model/dimension at a time. A model change
    requires an explicit index migration; a per-source processing job cannot do it.
    """
    model = settings.model_for_role("embedding")
    indexed_models = set(session.scalars(select(VectorDocument.embedding_model).distinct()))
    if indexed_models and indexed_models != {model}:
        raise ConfigurationError(
            "embedding model differs from the existing vector index; use the existing model "
            "until an explicit index migration is available"
        )
