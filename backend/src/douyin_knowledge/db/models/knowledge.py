"""Part D - Source-level knowledge (sections 18-21)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Float

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class KnowledgeItem(Base):
    """One interpreted view of a source, per processing run.

    Currency is derived from ``source_processing_state.current_processing_run_id``;
    there is deliberately no ``is_current`` flag to drift out of sync.

    NOTE: The write path for this table is not implemented. The schema exists but no
    code currently creates KnowledgeItem rows. This is scaffolding for a future feature,
    not a working abstraction.
    """

    __tablename__ = "knowledge_items"
    __table_args__ = (
        UniqueConstraint("processing_run_id", name="uq_knowledge_items_processing_run_id"),
        Index("ix_knowledge_items_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ki"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    generated_title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    key_points_json: Mapped[list[Any] | None] = mapped_column(JsonText)
    processing_level: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class KnowledgeItemLabel(Base):
    """Two-axis Domain x Form labels plus semantic routing labels."""

    __tablename__ = "knowledge_item_labels"
    __table_args__ = (Index("ix_knowledge_item_labels_type_value", "label_type", "value"),)

    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), primary_key=True
    )
    label_type: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    confidence: Mapped[float | None] = mapped_column(Float)


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("normalized_name", name="uq_topics_normalized_name"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("top"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class KnowledgeItemTopic(Base):
    __tablename__ = "knowledge_item_topics"

    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[str] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
