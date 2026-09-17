"""Normalized capture DTOs.

These are the *only* shapes the rest of the system sees. Every provider is
responsible for mapping its own wire format into these, so no Douyin-specific field
name ever leaks past this package (AGENTS 6).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["video", "image_album", "article", "image", "other"]
Availability = Literal["available", "unavailable", "unknown"]
AssetKind = Literal["video", "audio", "image", "keyframe", "subtitle", "other"]


class CapturedCreator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_creator_id: str
    display_name: str | None = None
    handle: str | None = None
    profile_url: str | None = None
    avatar_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CapturedCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_collection_id: str
    name: str
    description: str | None = None
    item_count: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CapturedMedia(BaseModel):
    """A downloadable artifact. URLs are provider-signed and often short-lived, which
    is exactly why the pipeline resolves them lazily instead of at sync time."""

    model_config = ConfigDict(extra="forbid")

    kind: AssetKind
    url: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    byte_size: int | None = None
    language: str | None = None
    text: str | None = Field(
        default=None, description="Inline content for native subtitles when the provider returns it"
    )


class CapturedSource(BaseModel):
    """One captured external item, provider-neutral."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    external_id: str
    source_type: SourceType = "video"
    title: str | None = None
    caption_raw: str | None = None
    source_url: str | None = None
    cover_url: str | None = None
    published_at_ms: int | None = None
    saved_at_ms: int | None = None
    duration_ms: int | None = None
    availability: Availability = "available"
    creator: CapturedCreator | None = None
    hashtags: list[str] = Field(default_factory=list)
    statistics: dict[str, int] = Field(default_factory=dict)
    media: list[CapturedMedia] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    def text_signal(self) -> str:
        """Everything textual known at sync time. Feeds metadata-phase policy rules."""
        parts = [self.title or "", self.caption_raw or ""]
        parts.extend(f"#{tag}" for tag in self.hashtags)
        return "\n".join(p for p in parts if p)


class SourcePage(BaseModel):
    """One page of a paginated collection listing."""

    model_config = ConfigDict(extra="forbid")

    sources: list[CapturedSource] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str | None = None
    requires_credentials: bool = False
