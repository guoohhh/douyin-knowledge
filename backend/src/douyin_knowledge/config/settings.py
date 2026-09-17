"""Application settings.

Three classes of configuration, per ARCHITECTURE.md section 23:

1. non-secret runtime config  -> env vars / .env, defaults here
2. secrets                    -> env vars only, never persisted to SQLite
3. user product settings      -> ``app_settings`` table (see db.repositories.settings)
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

VectorBackend = Literal["numpy", "lancedb"]
ProviderKind = Literal["fake", "openai", "openai_compatible", "local_whisper"]


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
    llm_provider: ProviderKind = "fake"
    embedding_provider: ProviderKind = "fake"
    asr_provider: ProviderKind = "fake"
    ocr_provider: ProviderKind = "fake"
    vision_provider: ProviderKind = "fake"

    openai_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str | None = None
    openai_timeout_s: float = 120.0

    # model roles (ARCHITECTURE.md section 16)
    triage_model: str = "fake-small"
    extraction_model: str = "fake-large"
    query_planner_model: str = "fake-small"
    answer_model: str = "fake-large"
    wiki_router_model: str = "fake-small"
    wiki_integration_model: str = "fake-large"
    vision_model: str = "fake-vision"
    embedding_model: str = "fake-embed"
    embedding_dim: int = 256
    asr_model: str = "fake-asr"
    ocr_model: str = "fake-ocr"

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

    def uses_real_providers(self) -> bool:
        return any(
            p != "fake"
            for p in (
                self.llm_provider,
                self.embedding_provider,
                self.asr_provider,
                self.ocr_provider,
                self.vision_provider,
            )
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
