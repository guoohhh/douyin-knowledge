"""Part A - Captured sources (PHYSICAL_SCHEMA sections 4-9)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.ids import new_id
from douyin_knowledge.db.base import Base, JsonText


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (
        UniqueConstraint("platform", "external_creator_id", name="uq_creators_platform_external"),
        Index("ix_creators_platform_display_name", "platform", "display_name"),
        Index("ix_creators_handle", "handle"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("cre"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_creator_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    handle: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    first_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    last_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)

    sources: Mapped[list[Source]] = relationship(back_populates="creator")


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint(
            "platform", "external_collection_id", name="uq_collections_platform_external"
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("col"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_collection_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText)
    first_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    last_synced_at_ms: Mapped[int | None] = mapped_column(Integer)


class Source(Base):
    """One captured external item. Holds *no* AI-derived fields (KM-001)."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_sources_platform_external_id"),
        Index("ix_sources_creator_id", "creator_id"),
        Index("ix_sources_saved_at_ms", "saved_at_ms"),
        Index("ix_sources_source_type", "source_type"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("src"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    creator_id: Mapped[str | None] = mapped_column(ForeignKey("creators.id"))
    title: Mapped[str | None] = mapped_column(Text)
    caption_raw: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    published_at_ms: Mapped[int | None] = mapped_column(Integer)
    saved_at_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    availability: Mapped[str] = mapped_column(Text, nullable=False, default="available")
    latest_snapshot_id: Mapped[str | None] = mapped_column(Text)
    first_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    last_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    locally_deleted_at_ms: Mapped[int | None] = mapped_column(Integer)

    creator: Mapped[Creator | None] = relationship(back_populates="sources")


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        Index("ix_source_snapshots_source_fetched", "source_id", "fetched_at_ms"),
        Index("ix_source_snapshots_source_hash", "source_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("snap"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    fetched_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)


class SourceCollectionMembership(Base):
    """Membership is soft-removed via ``is_present`` so past organization survives."""

    __tablename__ = "source_collection_memberships"

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    first_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    last_seen_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    is_present: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SourceAsset(Base):
    __tablename__ = "source_assets"
    __table_args__ = (
        Index("ix_source_assets_source_type", "source_id", "asset_type"),
        Index("ix_source_assets_retention_expires", "retention_class", "expires_at_ms"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("ast"))
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    retention_class: Mapped[str] = mapped_column(Text, nullable=False, default="cache")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    timestamp_ms: Mapped[int | None] = mapped_column(Integer)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=now_ms)
    expires_at_ms: Mapped[int | None] = mapped_column(Integer)
