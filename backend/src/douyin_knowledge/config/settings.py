"""Application settings.

Three classes of configuration, per ARCHITECTURE.md section 23:

1. non-secret runtime config  -> env vars / .env, defaults here
2. secrets                    -> env vars only, never persisted to SQLite
3. user product settings      -> ``app_settings`` table (see db.repositories.settings)
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

VectorBackend = Literal["numpy", "lancedb"]


class Settings(BaseSettings):
    """Runtime configuration. Every field is overridable by env var ``DK_<NAME>``."""

    model_config = SettingsConfigDict(
        env_prefix="DK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # ---- paths -----------------------------------------------------------
    data_dir: Path = Field(default=Path("./data"), description="Root for all local runtime state")
    database_url: str | None = Field(
        default=None, description="Override the SQLite URL; defaults to <data_dir>/app.db"
    )

    # ---- server ----------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])
    serve_frontend: bool = Field(
        default=True, description="Serve the built frontend bundle from the API when present"
    )

    # ---- sqlite ----------------------------------------------------------
    sqlite_busy_timeout_ms: int = 5000
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"
    sql_echo: bool = False

    # ---- capture ---------------------------------------------------------
    capture_provider: Literal["fixture", "douyin"] = Field(
        default="fixture",
        description="'fixture' needs no credentials; 'douyin' talks to the sidecar",
    )
    douyin_sidecar_url: str = "http://127.0.0.1:8000"
    douyin_sidecar_api_key: str | None = Field(default=None, repr=False)
    douyin_sidecar_timeout_s: float = 30.0
    capture_fixture_dir: Path | None = Field(
        default=None, description="Defaults to the packaged demo fixture set"
    )

    # ---- ai providers ----------------------------------------------------
    ai_provider: Literal["mock", "openai"] = Field(
        default="mock",
        description="Primary AI provider: 'mock' for testing, 'openai' for production",
    )
    asr_provider: Literal["mock", "openai"] | None = Field(
        default=None,
        description="Override ASR provider; defaults to ai_provider",
    )
    ocr_provider: Literal["mock", "openai"] | None = Field(
        default=None,
        description="Override OCR provider; defaults to ai_provider",
    )
    embedding_provider: Literal["mock", "openai"] | None = Field(
        default=None,
        description="Override embedding provider; defaults to ai_provider",
    )

    openai_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str | None = None
    openai_timeout_s: float = 120.0
    openai_chat_model: str | None = Field(
        default="gpt-4o-mini",
        description="Model for chat and structured extraction",
    )
    openai_vision_model: str | None = Field(
        default="gpt-4o",
        description="Model for vision understanding",
    )
    openai_embedding_model: str | None = Field(
        default="text-embedding-3-small",
        description="Model for vector embeddings",
    )

    # ---- model roles (ARCHITECTURE.md section 16) -------------------------
    # Each role is an *override*: left unset, the name is derived from the active
    # provider by `model_for_role`. Hardcoding "fake-small" style defaults here was
    # actively misleading -- the strings were never sent to any provider, so a
    # processing run stamped `extraction_model="fake-large"` while genuinely calling
    # gpt-4o-mini, and the provenance record lied about which model produced a claim.
    triage_model: str | None = None
    extraction_model: str | None = None
    query_planner_model: str | None = None
    answer_model: str | None = None
    wiki_router_model: str | None = None
    wiki_integration_model: str | None = None
    vision_model: str | None = None
    embedding_model: str | None = None
    asr_model: str | None = None
    ocr_model: str | None = None
    embedding_dim: int = 256

    # ---- processing ------------------------------------------------------
    default_desired_level: int = Field(default=2, ge=0, le=4)
    max_processing_level: int = Field(default=3, ge=0, le=4)
    processor_version: str = "0.1.0"
    knowledge_schema_version: str = "knowledge-v1"
    media_retention: Literal["cache", "retained"] = "cache"

    # ---- worker ----------------------------------------------------------
    worker_poll_interval_s: float = 1.0
    worker_lease_ttl_s: int = 600
    worker_batch_sleep_s: float = 0.05
    job_max_attempts: int = 3
    job_backoff_base_s: float = 5.0
    job_backoff_max_s: float = 900.0

    # ---- retrieval -------------------------------------------------------
    vector_backend: VectorBackend = "numpy"
    fts_tokenizer: Literal["unicode61", "trigram"] = "unicode61"
    fts_segment_cjk: bool = Field(
        default=True,
        description="Pre-segment CJK text with jieba; required for usable unicode61 recall",
    )
    retrieval_candidate_limit: int = 40
    retrieval_per_surface_limit: int = 25
    retrieval_max_per_source: int = 3
    retrieval_evidence_budget: int = 24
    personal_first: bool = True
    enable_query_enrichment: bool = True

    # ---- wiki ------------------------------------------------------------
    wiki_route_candidate_limit: int = 8
    wiki_quality_close_threshold: int = 3

    # ---- observability ---------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{(self.data_dir / 'app.db').resolve()}"

    @property
    def db_path(self) -> Path | None:
        """Filesystem path of the SQLite database, or None for in-memory."""
        url = self.resolved_database_url
        if ":memory:" in url:
            return None
        return Path(url.split("///", 1)[-1])

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "vectors"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.media_dir / "cache",
            self.media_dir / "pinned",
            self.evidence_dir / "frames",
            self.evidence_dir / "artifacts",
            self.vector_dir,
            self.export_dir / "markdown",
            self.log_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---- model role resolution -------------------------------------------
    #: Which concrete provider setting backs each role. Roles are a *product*
    #: vocabulary (ARCHITECTURE 16); providers are an infrastructure one. Keeping the
    #: mapping in one table means adding a provider does not mean touching ten
    #: call sites that each guess at a model name.
    _ROLE_KIND: ClassVar[dict[str, str]] = {
        "triage": "chat",
        "extraction": "chat",
        "query_planner": "chat",
        "answer": "chat",
        "wiki_router": "chat",
        "wiki_integration": "chat",
        "vision": "vision",
        "embedding": "embedding",
        "asr": "asr",
        "ocr": "ocr",
    }

    def provider_for_role(self, role: str) -> str:
        kind = self._ROLE_KIND.get(role, "chat")
        if kind == "embedding":
            return self.embedding_provider or self.ai_provider
        if kind == "asr":
            return self.asr_provider or self.ai_provider
        if kind == "ocr":
            return self.ocr_provider or self.ai_provider
        return self.ai_provider

    def model_for_role(self, role: str) -> str:
        """Resolve the model name to record and to send for a given role.

        An explicit per-role override always wins; otherwise the name comes from the
        provider actually in use, so what gets written to ``processing_runs.models_json``
        is what was really called.
        """
        override = getattr(self, f"{role}_model", None)
        if override:
            return str(override)

        provider = self.provider_for_role(role)
        if provider == "mock":
            return f"mock-{role}"

        kind = self._ROLE_KIND.get(role, "chat")
        if kind == "embedding":
            return self.openai_embedding_model or "text-embedding-3-small"
        if kind == "vision":
            return self.openai_vision_model or "gpt-4o"
        if kind == "asr":
            return "whisper-1"
        if kind == "ocr":
            return self.openai_vision_model or "gpt-4o"
        return self.openai_chat_model or "gpt-4o-mini"

    def uses_real_providers(self) -> bool:
        """True when any capability is wired to something that costs money or leaves
        the machine. Demo mode (everything mock) must stay detectable."""
        return any(
            self.provider_for_role(role) != "mock" for role in self._ROLE_KIND
        )

    def redacted(self) -> dict[str, object]:
        """Settings dump safe for logs and the settings API. Secrets become booleans."""
        data = self.model_dump(mode="json")
        for secret in ("openai_api_key", "douyin_sidecar_api_key"):
            data[secret] = bool(getattr(self, secret))
        return data


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
