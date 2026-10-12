"""Persistent, non-secret selection of Douyin capture targets.

Capture Scope decides what enters the local corpus. Processing Policy is applied
later and is never edited here. The first read writes a one-time explicit setting
from collections already captured in this database; migrations never guess intent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Collection
from douyin_knowledge.db.models.ops import AppSetting

if TYPE_CHECKING:
    from douyin_knowledge.config.settings import Settings

CAPTURE_SCOPE_KEY = "capture.scope.douyin"


class CaptureScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    platform: Literal["douyin"] = "douyin"
    default_favorites: bool = False
    named_collection_ids: list[str] = Field(default_factory=list)

    @field_validator("named_collection_ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("collection ids must be nonempty")
        return list(dict.fromkeys(cleaned))

    def selects_named(self, external_id: str) -> bool:
        return external_id in self.named_collection_ids


def get_capture_scope(
    session: Session,
    *,
    provider_kind: str = "douyin",
    fixture_collection_ids: list[str] | None = None,
) -> CaptureScope:
    """Read the setting, bootstrapping exactly once from captured collections."""
    row = session.get(AppSetting, CAPTURE_SCOPE_KEY)
    if row is None:
        # Legacy demo databases used `fixture` for Collection.platform. Include
        # those only while running that provider: fixture ids are not real Douyin
        # collection ids and must not be sent to a later real sidecar connection.
        platforms = ("douyin", "fixture") if provider_kind == "fixture" else ("douyin",)
        ids = session.scalars(
            select(Collection.external_collection_id)
            .where(Collection.platform.in_(platforms))
            .order_by(Collection.external_collection_id)
        ).all()
        fallback = fixture_collection_ids if provider_kind == "fixture" else None
        scope = CaptureScope(named_collection_ids=list(dict.fromkeys(ids or fallback or [])))
        session.add(
            AppSetting(
                key=CAPTURE_SCOPE_KEY,
                value_json=scope.model_dump(mode="json"),
                updated_at_ms=now_ms(),
            )
        )
        session.flush()
        return scope
    return CaptureScope.model_validate(row.value_json)


def initialize_capture_scope(session: Session, settings: Settings) -> CaptureScope:
    """Initialize a fresh fixture DB from its configured local provider exactly once.

    Existing captured rows win. Real Douyin never reads a provider to infer scope.
    Once a setting exists, including an explicit empty one, no provider is consulted.
    """
    fixture_ids: list[str] | None = None
    if settings.capture_provider == "fixture" and session.get(AppSetting, CAPTURE_SCOPE_KEY) is None:
        captured = session.scalar(
            select(Collection.id).where(Collection.platform.in_(("douyin", "fixture"))).limit(1)
        )
        if captured is None:
            from douyin_knowledge.capture.registry import get_capture_provider

            fixture_ids = [
                item.external_collection_id
                for item in get_capture_provider(settings).list_collections()
            ]
    return get_capture_scope(
        session,
        provider_kind=settings.capture_provider,
        fixture_collection_ids=fixture_ids,
    )


def set_capture_scope(session: Session, scope: CaptureScope) -> CaptureScope:
    row = session.get(AppSetting, CAPTURE_SCOPE_KEY)
    if row is None:
        row = AppSetting(key=CAPTURE_SCOPE_KEY, value_json=scope.model_dump(mode="json"))
        session.add(row)
    else:
        row.value_json = scope.model_dump(mode="json")
        row.updated_at_ms = now_ms()
    session.flush()
    return scope


__all__ = [
    "CAPTURE_SCOPE_KEY", "CaptureScope", "get_capture_scope",
    "initialize_capture_scope", "set_capture_scope",
]
