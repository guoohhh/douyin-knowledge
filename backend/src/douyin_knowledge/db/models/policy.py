"""Part B - Processing policy and operational state (sections 10-12)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class ProcessingRule(Base):
    """Precedence is resolved in application logic, never encoded in SQL (PP-004)."""

    __tablename__ = "processing_rules"
    __table_args__ = (
        Index("ix_processing_rules_enabled_type", "is_enabled", "rule_type"),
        Index("ix_processing_rules_target_source_id", "target_source_id"),
        Index("ix_processing_rules_target_creator_id", "target_creator_id"),
        Index("ix_processing_rules_target_collection_id", "target_collection_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("rule"))
    name: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    target_creator_id: Mapped[str | None] = mapped_column(ForeignKey("creators.id"))
    target_collection_id: Mapped[str | None] = mapped_column(ForeignKey("collections.id"))
    matcher_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class PolicyDecision(Base):
    """Audit log: why a source was processed or skipped."""

    __tablename__ = "policy_decisions"
    __table_args__ = (Index("ix_policy_decisions_source_created", "source_id", "created_at_ms"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("pol"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("processing_rules.id", ondelete="SET NULL")
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    explanation_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    model_name: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class SourceProcessingState(Base):
    """The single mutable operational row per source.

    ``current_processing_run_id`` is the pointer that makes reprocessing versioned
    (AGENTS 4.5). Retrieval MUST filter claims/chunks through it (DB-004).
    """

    __tablename__ = "source_processing_state"
    __table_args__ = (
        Index("ix_source_processing_state_status", "processing_status"),
        Index("ix_source_processing_state_policy_action", "current_policy_action"),
    )

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    current_policy_action: Mapped[str] = mapped_column(Text, nullable=False, default="process")
    current_policy_decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_decisions.id")
    )
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    desired_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    achieved_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Intentionally not an FK: processing_runs is created after the state row and a
    # circular FK pair makes SQLite batch migrations painful. Integrity is enforced
    # in the repository layer.
    current_processing_run_id: Mapped[str | None] = mapped_column(Text)
    last_success_at_ms: Mapped[int | None] = mapped_column(Integer)
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class SourceTriage(Base):
    """Cached cheap content-type classification (DEC-016).

    Derived state: one row per source, recomputable from the source's own metadata, and
    keyed to the metadata it was computed from by ``signal_fingerprint`` so an unchanged
    re-sync neither reclassifies nor re-pays for a model call.

    No history. A triage label is a routing hint, not knowledge (PROCESSING_POLICY.md
    5.4), and the decision it influenced is already recorded in ``policy_decisions``.
    """

    __tablename__ = "source_triage"
    __table_args__ = (Index("ix_source_triage_content_type", "content_type"),)

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(Text)
    cues_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    signal_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
