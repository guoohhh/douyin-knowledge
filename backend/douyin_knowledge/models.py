from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid() -> str:
    return uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    platform: Mapped[str] = mapped_column(String, default="douyin")
    external_id: Mapped[str] = mapped_column(String)
    capture_origin: Mapped[str] = mapped_column(String, default="import")
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    creator_id: Mapped[str] = mapped_column(String, default="")
    creator_name: Mapped[str] = mapped_column(String, default="")
    collection: Mapped[str] = mapped_column(String, default="")
    semantic_type: Mapped[str] = mapped_column(String, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    policy_action: Mapped[str] = mapped_column(String, default="pending")
    policy_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="pending")
    current_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content_checksum: Mapped[str] = mapped_column(String, default="")
    processed_checksum: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (UniqueConstraint("source_id", "checksum"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    checksum: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceAsset(Base):
    __tablename__ = "source_assets"
    __table_args__ = (UniqueConstraint("source_id", "kind"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String)
    remote_url: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourceCollectionMembership(Base):
    __tablename__ = "source_collection_memberships"
    __table_args__ = (UniqueConstraint("source_id", "collection_name"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    collection_name: Mapped[str] = mapped_column(String)


class SyncEvent(Base):
    __tablename__ = "sync_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    total: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProcessingRule(Base):
    __tablename__ = "processing_rules"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    dimension: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String, default="process")
    status: Mapped[str] = mapped_column(String, default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="running")
    level: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String, default="local")
    model_name: Mapped[str] = mapped_column(String, default="")
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Evidence(Base):
    __tablename__ = "evidence_units"
    __table_args__ = (UniqueConstraint("source_id", "kind", "text"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), unique=True
    )
    summary: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String, default="other")
    form: Mapped[str] = mapped_column(String, default="reference")
    coverage: Mapped[str] = mapped_column(String, default="text_only")


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("kind", "normalized_name"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="concept")


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    surface: Mapped[str] = mapped_column(String)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_units.id"))


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"))
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_units.id"))
    predicate: Mapped[str] = mapped_column(String, default="states")
    value: Mapped[str] = mapped_column(Text)
    attribution: Mapped[str] = mapped_column(String, default="creator")


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"
    __table_args__ = (UniqueConstraint("page_id", "number"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    page_id: Mapped[str] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WikiSupport(Base):
    __tablename__ = "wiki_supports"
    __table_args__ = (UniqueConstraint("revision_id", "claim_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    revision_id: Mapped[str] = mapped_column(ForeignKey("wiki_revisions.id", ondelete="CASCADE"))
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"))


class WikiQualityEvent(Base):
    __tablename__ = "wiki_quality_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    page_id: Mapped[str] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"))
    issue: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserState(Base):
    __tablename__ = "user_states"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), unique=True)
    state: Mapped[str] = mapped_column(String)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VectorDocument(Base):
    __tablename__ = "vector_documents"
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String)
    vector: Mapped[list] = mapped_column(JSON)


class SourceAnnotation(Base):
    __tablename__ = "source_annotations"
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    payload: Mapped[list] = mapped_column(JSON)
