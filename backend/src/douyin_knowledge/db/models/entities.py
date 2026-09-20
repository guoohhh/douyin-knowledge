"""Part E - Entities and claims (sections 22-28)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Float

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class Entity(Base):
    """Canonical object. Never holds creator opinions as authoritative columns."""

    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_type_normalized_name", "entity_type", "normalized_name"),
        Index("ix_entities_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ent"))
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    subtype: Mapped[str | None] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    profile_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    merged_into_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "normalized_alias", name="uq_entity_aliases_entity_alias"),
        Index("ix_entity_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("alias"))
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="model")
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class EntityExternalId(Base):
    """Strong identity signal - the only evidence that justifies an automatic merge."""

    __tablename__ = "entity_external_ids"
    __table_args__ = (
        UniqueConstraint("namespace", "value", name="uq_entity_external_ids_namespace_value"),
        Index("ix_entity_external_ids_entity_id", "entity_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("xid"))
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class EntityMention(Base):
    """Source-local mention. Never deleted after resolution (ENT-005)."""

    __tablename__ = "entity_mentions"
    __table_args__ = (
        Index("ix_entity_mentions_source_run", "source_id", "processing_run_id"),
        Index("ix_entity_mentions_normalized_text", "normalized_text"),
        Index("ix_entity_mentions_resolved_entity_id", "resolved_entity_id"),
        Index("ix_entity_mentions_resolution_status", "resolution_status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("men"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    mention_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type_hint: Mapped[str | None] = mapped_column(Text)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    resolved_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    resolution_status: Mapped[str] = mapped_column(Text, nullable=False, default="unresolved")
    resolution_confidence: Mapped[float | None] = mapped_column(Float)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class EntityMentionEvidence(Base):
    __tablename__ = "entity_mention_evidence"

    entity_mention_id: Mapped[str] = mapped_column(
        ForeignKey("entity_mentions.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="CASCADE"), primary_key=True
    )


class Claim(Base):
    """Atomic source-attributed assertion. A Claim is never a global fact (KM-003)."""

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_subject_entity_predicate", "subject_entity_id", "predicate"),
        Index("ix_claims_subject_topic_predicate", "subject_topic_id", "predicate"),
        Index("ix_claims_predicate", "predicate"),
        Index("ix_claims_source_id", "source_id"),
        Index("ix_claims_processing_run_id", "processing_run_id"),
        CheckConstraint(
            "subject_entity_id IS NOT NULL OR subject_topic_id IS NOT NULL "
            "OR subject_text IS NOT NULL",
            name="claims_subject_present",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("clm"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    subject_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    subject_topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    subject_text: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    object_topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[float | None] = mapped_column(Float)
    value_json: Mapped[Any | None] = mapped_column(JsonText)
    unit: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    claim_kind: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_type: Mapped[str] = mapped_column(Text, nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    valid_from_ms: Mapped[int | None] = mapped_column(Integer)
    valid_to_ms: Mapped[int | None] = mapped_column(Integer)
    grounding_status: Mapped[str | None] = mapped_column(Text)
    grounding_json: Mapped[Any | None] = mapped_column(JsonText)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class ClaimEvidence(Base):
    """Claim -> ClaimEvidence -> EvidenceUnit -> Source is the provenance spine."""

    __tablename__ = "claim_evidence"

    claim_id: Mapped[str] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="CASCADE"), primary_key=True
    )
    support_role: Mapped[str] = mapped_column(Text, primary_key=True, default="supports")
