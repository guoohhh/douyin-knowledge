"""Part I - Conversation and citations (sections 44-47)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_updated_at_ms", "updated_at_ms"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("cnv"))
    title: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at_ms"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_scope: Mapped[str | None] = mapped_column(Text)
    query_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    response_meta_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class MessageCitation(Base):
    """Structured citations - never IDs buried in Markdown (RET-008)."""

    __tablename__ = "message_citations"
    __table_args__ = (
        Index("ix_message_citations_message_ordinal", "message_id", "ordinal"),
        CheckConstraint(
            "source_id IS NOT NULL OR evidence_id IS NOT NULL OR claim_id IS NOT NULL "
            "OR wiki_page_id IS NOT NULL",
            name="message_citations_target_present",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("cit"))
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_units.id", ondelete="SET NULL")
    )
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("claims.id", ondelete="SET NULL"))
    wiki_page_id: Mapped[str | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL")
    )
    label: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class ConversationState(Base):
    """Short-term follow-up context. Not long-term personal knowledge."""

    __tablename__ = "conversation_state"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    state_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
