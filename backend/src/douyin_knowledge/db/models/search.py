"""Part H - Search and vector index tracking (sections 41-43). All derived state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class SearchDocument(Base):
    """Unified rebuildable text projection.

    ``search_fts`` is an FTS5 external-content table over this one, keyed on rowid,
    so this model declares an explicit integer ``rowid`` column.
    """

    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint("doc_type", "object_id", name="uq_search_documents_doc_type_object_id"),
        Index("ix_search_documents_updated_at_ms", "updated_at_ms"),
    )

    rowid: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(Text, nullable=False, unique=True, default=lambda: new_id("sd"))
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)


class VectorDocument(Base):
    """Bookkeeping for the external vector index; stale detection by content hash."""

    __tablename__ = "vector_documents"
    __table_args__ = (
        UniqueConstraint(
            "doc_type", "object_id", "embedding_model", name="uq_vector_documents_identity"
        ),
        Index("ix_vector_documents_vector_key", "vector_key"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("vd"))
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_key: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
