"""Read-only Capture Target discovery projected beside persisted local scope/state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.capture.registry import get_capture_provider
from douyin_knowledge.capture.scope import CaptureScope, initialize_capture_scope
from douyin_knowledge.config.settings import Settings
from douyin_knowledge.core.errors import DKError
from douyin_knowledge.db.models.capture import Collection, DefaultFavoritesSyncState


class DiscoveryStatus(BaseModel):
    state: Literal["ok", "error"]
    error_code: str | None = None
    message: str | None = None


class DefaultFavoritesTarget(BaseModel):
    available: bool
    selected: bool
    last_completed_at_ms: int | None


class NamedCollectionTarget(BaseModel):
    external_collection_id: str
    name: str | None
    item_count: int | None
    discovered: bool
    locally_observed: bool
    selected: bool
    last_synced_at_ms: int | None


class CaptureTargets(BaseModel):
    scope: CaptureScope
    discovery: DiscoveryStatus
    default_favorites: DefaultFavoritesTarget
    named_collections: list[NamedCollectionTarget]


def discover_capture_targets(session: Session, settings: Settings) -> CaptureTargets:
    """Read named target metadata; never read target contents or change selection.

    A discovery fault is partial success: saved selection and local sync history
    remain useful to settings clients. No empty upstream list is asserted on error.
    """
    scope = initialize_capture_scope(session, settings)
    platforms = ("douyin", "fixture") if settings.capture_provider == "fixture" else ("douyin",)
    local = session.scalars(
        select(Collection)
        .where(Collection.platform.in_(platforms))
        .order_by(Collection.platform, Collection.external_collection_id)
    ).all()
    targets = {
        row.external_collection_id: NamedCollectionTarget(
            external_collection_id=row.external_collection_id,
            name=row.name,
            item_count=None,
            discovered=False,
            locally_observed=True,
            selected=scope.selects_named(row.external_collection_id),
            last_synced_at_ms=row.last_synced_at_ms,
        )
        for row in local
    }
    for external_id in scope.named_collection_ids:
        targets.setdefault(
            external_id,
            NamedCollectionTarget(
                external_collection_id=external_id,
                name=None,
                item_count=None,
                discovered=False,
                locally_observed=False,
                selected=True,
                last_synced_at_ms=None,
            ),
        )

    provider = None
    try:
        provider = get_capture_provider(settings)
        discovered = provider.list_collections()
    except DKError as exc:
        status = DiscoveryStatus(
            state="error", error_code=exc.code, message="Named collection discovery failed"
        )
    except Exception:
        status = DiscoveryStatus(
            state="error", error_code="internal_error", message="Named collection discovery failed"
        )
    else:
        status = DiscoveryStatus(state="ok")
        for item in discovered:
            previous = targets.get(item.external_collection_id)
            targets[item.external_collection_id] = NamedCollectionTarget(
                external_collection_id=item.external_collection_id,
                name=item.name,
                item_count=item.item_count,
                discovered=True,
                locally_observed=previous.locally_observed if previous else False,
                selected=scope.selects_named(item.external_collection_id),
                last_synced_at_ms=previous.last_synced_at_ms if previous else None,
            )
    finally:
        # Discovery creates a short-lived sidecar HTTP client via the registry.
        # It owns that client; target sync jobs manage their own provider lifetime.
        if isinstance(provider, DouyinCaptureProvider):
            provider.close()

    default_state = session.get(DefaultFavoritesSyncState, "douyin")
    return CaptureTargets(
        scope=scope,
        discovery=status,
        default_favorites=DefaultFavoritesTarget(
            available=settings.capture_provider in ("fixture", "douyin"),
            selected=scope.default_favorites,
            last_completed_at_ms=(
                default_state.last_completed_at_ms if default_state is not None else None
            ),
        ),
        named_collections=sorted(targets.values(), key=lambda target: target.external_collection_id),
    )
