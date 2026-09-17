"""Part C - Processing runs, evidence and retrieval chunks (sections 13-17)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Float

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        Index("ix_processing_runs_source_started", "source_id", "started_at_ms"),
        Index("ix_processing_runs_status", "status"),
        # At most one non-terminal run per source: without this a duplicated
        # process_source job races on current_processing_run_id.
        Index(
            "ux_processing_runs_active",
            "source_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("run"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    run_kind: Mapped[str] = mapped_column(Text, nullable=False)
    processor_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    target_level: Mapped[int] = mapped_column(Integer, nullable=False)
    achieved_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    models_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    started_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    finished_at_ms: Mapped[int | None] = mapped_column(Integer)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)


class EvidenceUnit(Base):
    """Citation-sized durable evidence. Owned by the source, *not* by a run."""

    __tablename__ = "evidence_units"
    __table_args__ = (
        Index("ix_evidence_units_source_kind", "source_id", "kind"),
        Index("ix_evidence_units_content_hash", "content_hash"),
        UniqueConstraint("source_id", "kind", "content_hash", name="uq_evidence_source_kind_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ev"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("source_assets.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    raw_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    observation_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    language: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class ProcessingRunEvidence(Base):
    """Join table so reprocessing can reuse expensive evidence (transcript, OCR)."""

    __tablename__ = "processing_run_evidence"

    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="CASCADE"), primary_key=True
    )
    usage_role: Mapped[str] = mapped_column(Text, nullable=False, default="input")


class RetrievalChunk(Base):
    """Retrieval-sized text. Never a citation unit - it points back to evidence."""

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id", "chunk_type", "ordinal", name="uq_retrieval_chunk_run_type_ordinal"
        ),
        Index("ix_retrieval_chunks_source_id", "source_id"),
        Index("ix_retrieval_chunks_run_id", "processing_run_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("chk"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_type: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class RetrievalChunkEvidence(Base):
    __tablename__ = "retrieval_chunk_evidence"

    retrieval_chunk_id: Mapped[str] = mapped_column(
        ForeignKey("retrieval_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="CASCADE"), primary_key=True
    )
