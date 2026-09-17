"""Part F - Personal state (sections 29-31). Never mixed with creator claims (KM-005)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Float

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class EntityUserState(Base):
    __tablename__ = "entity_user_states"
    __table_args__ = (Index("ix_entity_user_states_state", "state"),)

    entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    attributes_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    first_action_at_ms: Mapped[int | None] = mapped_column(Integer)
    last_action_at_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class KnowledgeItemUserState(Base):
    __tablename__ = "knowledge_item_user_states"

    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    attributes_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class UserAnnotation(Base):
    """Append-only personal history."""

    __tablename__ = "user_annotations"
    __table_args__ = (
        Index("ix_user_annotations_entity_id", "entity_id"),
        Index("ix_user_annotations_source_id", "source_id"),
        Index("ix_user_annotations_type_created", "annotation_type", "created_at_ms"),
        CheckConstraint(
            "source_id IS NOT NULL OR entity_id IS NOT NULL OR knowledge_item_id IS NOT NULL",
            name="user_annotations_target_present",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ann"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    knowledge_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE")
    )
    annotation_type: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[Any | None] = mapped_column(JsonText)
    occurred_at_ms: Mapped[int | None] = mapped_column(Integer)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
