"""Persist captured items into the provenance spine.

This is the boundary where provider-shaped data (`CapturedSource`) becomes
database state. Everything written here is *observed*, never inferred: no AI
field is set, no processing level is claimed. That is what KM-001 means by
keeping `Source` free of derived data.

Three behaviors matter more than they look:

* **Sync is idempotent.** Re-running it over an unchanged collection produces no
  new rows and no new snapshots, because the item is keyed on
  ``(platform, external_id)`` and its payload is compared by content hash. Users
  will re-sync constantly; that must be cheap and must not inflate history.

* **Disappearance is not deletion.** An item missing from a collection listing
  gets ``is_present = 0`` on its membership, keeping the record that it was once
  there. Deleting the row would destroy the only evidence of past organization,
  and "what did I have saved in March" is a question this system is supposed to
  answer.

* **Absence is only concluded from a complete listing.** Marking memberships absent
  is the one destructive thing here, so it is gated on ``prune_missing``, which
  ``sync_collection`` sets only when the page walk finished. See
  ``capture/pagination.py`` for why that boolean is isolated.

* **Snapshots accumulate only on change.** Each *different* payload becomes a
  `SourceSnapshot`, which is how a caption edit or a price change in the
  description stays visible after the fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.capture.pagination import (
    DEFAULT_MAX_PAGES,
    CollectionWalker,
    log_walk,
)
from douyin_knowledge.config import get_settings
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.text import content_hash
from douyin_knowledge.db.models.capture import (
    Collection,
    Creator,
    Source,
    SourceAsset,
    SourceCollectionMembership,
    SourceSnapshot,
)
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.media.store import MediaStore, url_fingerprint
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.capture.models import (
        CapturedCollection,
        CapturedCreator,
        CapturedSource,
    )

logger = get_logger(__name__)


@dataclass
class SyncStats:
    collections: int = 0
    creators_created: int = 0
    sources_created: int = 0
    sources_updated: int = 0
    sources_unchanged: int = 0
    snapshots: int = 0
    assets: int = 0
    assets_superseded: int = 0
    memberships_removed: int = 0
    subtitles_captured: int = 0
    source_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "collections": self.collections,
            "creators_created": self.creators_created,
            "sources_created": self.sources_created,
            "sources_updated": self.sources_updated,
            "sources_unchanged": self.sources_unchanged,
            "snapshots": self.snapshots,
            "assets": self.assets,
            "memberships_removed": self.memberships_removed,
            "subtitles_captured": self.subtitles_captured,
        }


class CaptureSyncService:
    """Writes `CapturedSource` batches into the database idempotently.

    `platform` is held by the service rather than read off each item: collections
    and creators are provider-scoped concepts, so the provider identity belongs
    to the sync session, not to every payload.
    """

    def __init__(
        self,
        session: Session,
        *,
        platform: str = "douyin",
        media_store: MediaStore | None = None,
    ) -> None:
        self.session = session
        self.platform = platform
        # Sync never downloads; it only needs the store to *name* local destinations and
        # to delete superseded files. Defaulted so the many callers that do not care
        # about media paths stay unchanged.
        self.media_store = media_store or MediaStore(get_settings().media_dir)

    # -------------------------------------------------------------- collections

    def upsert_collection(self, captured: CapturedCollection) -> Collection:
        """Record the folder itself. Deliberately does *not* stamp
        `last_synced_at_ms`: this runs before the page walk, so stamping here would
        mean "we started reading it", while the API and UI present the field as when
        the collection was last successfully read through. `sync_sources` sets it, and
        only on a complete listing.
        """
        existing = self.session.scalars(
            select(Collection).where(
                Collection.platform == self.platform,
                Collection.external_collection_id == captured.external_collection_id,
            )
        ).first()

        if existing is None:
            collection = Collection(
                platform=self.platform,
                external_collection_id=captured.external_collection_id,
                name=captured.name,
                description=captured.description,
                raw_json=self._raw_of(captured),
            )
            self.session.add(collection)
            self.session.flush()
            return collection

        existing.name = captured.name
        existing.description = captured.description or existing.description
        return existing

    def upsert_creator(self, captured: CapturedCreator) -> Creator:
        existing = self.session.scalars(
            select(Creator).where(
                Creator.platform == self.platform,
                Creator.external_creator_id == captured.external_creator_id,
            )
        ).first()

        if existing is None:
            creator = Creator(
                platform=self.platform,
                external_creator_id=captured.external_creator_id,
                display_name=captured.display_name,
                handle=captured.handle,
                profile_url=captured.profile_url,
                avatar_url=captured.avatar_url,
                raw_json=self._raw_of(captured),
            )
            self.session.add(creator)
            self.session.flush()
            return creator

        existing.display_name = captured.display_name or existing.display_name
        existing.last_seen_at_ms = now_ms()
        return existing

    # ------------------------------------------------------------------ sources

    def sync_sources(
        self,
        sources: Sequence[CapturedSource],
        *,
        collection: Collection | None = None,
        prune_missing: bool = False,
        stats: SyncStats | None = None,
    ) -> SyncStats:
        """Upsert a batch of captured sources.

        `prune_missing` marks memberships absent for items not in this batch. It
        must only be set when `sources` is a *complete* listing of the
        collection, otherwise a partial page would mark the rest as removed.
        """
        stats = stats or SyncStats()
        seen_ids: list[str] = []

        for captured in sources:
            source, changed = self._upsert_source(captured, stats)
            seen_ids.append(source.id)
            stats.source_ids.append(source.id)

            if changed:
                self._write_snapshot(source, captured, stats)
                self._sync_assets(source, captured, stats)

            if collection is not None:
                self._touch_membership(source, collection)

        if collection is not None and prune_missing:
            stats.memberships_removed += self._mark_absent(collection, seen_ids)

        if collection is not None and prune_missing:
            # Only a complete listing earns this timestamp. `prune_missing` already
            # means "this batch is the whole collection", and stamping it after a
            # partial walk would report a full sync that never happened -- which is
            # also what the UI shows the user as "last updated".
            collection.last_synced_at_ms = now_ms()

        self.session.flush()
        logger.info("capture_sync_complete", extra=stats.as_dict())
        return stats

    def sync_collection(
        self,
        provider: Any,
        captured_collection: CapturedCollection,
        *,
        page_limit: int = 50,
        max_pages: int = DEFAULT_MAX_PAGES,
        stats: SyncStats | None = None,
    ) -> SyncStats:
        """Walk a provider collection and sync it, pruning only on a complete walk.

        The `try/finally` is the point of this method. Whatever ends the walk --
        a provider-stated end, a network failure on page 3, a cursor that stops
        advancing -- the pages already fetched get persisted, and pruning happens
        only if the walk is `safe_to_prune`. Upserts are idempotent and
        non-destructive, so keeping partial pages costs nothing and saves re-fetching
        them; marking memberships absent from a partial listing, by contrast, is
        unrecoverable without a full successful re-sync.

        An exception still propagates. The job that called us should fail and retry:
        a sync that swallowed a mid-walk failure would report success over a library
        it only partly read.
        """
        stats = stats or SyncStats()
        collection = self.upsert_collection(captured_collection)
        walker = CollectionWalker(
            provider,
            captured_collection.external_collection_id,
            page_limit=page_limit,
            max_pages=max_pages,
        )

        try:
            for _page_sources in walker.pages():
                pass
        finally:
            prune = walker.walk.safe_to_prune
            self.sync_sources(
                walker.sources,
                collection=collection,
                prune_missing=prune,
                stats=stats,
            )
            log_walk(walker.walk, pruned=prune)

        stats.collections += 1
        return stats

    def _upsert_source(
        self, captured: CapturedSource, stats: SyncStats
    ) -> tuple[Source, bool]:
        creator = self.upsert_creator(captured.creator) if captured.creator else None
        digest = self._payload_hash(captured)

        existing = self.session.scalars(
            select(Source).where(
                Source.platform == captured.platform,
                Source.external_id == captured.external_id,
            )
        ).first()

        if existing is None:
            source = Source(
                platform=captured.platform,
                external_id=captured.external_id,
                source_type=captured.source_type,
                creator_id=creator.id if creator else None,
                title=captured.title,
                caption_raw=captured.caption_raw,
                source_url=captured.source_url,
                cover_url=captured.cover_url,
                published_at_ms=captured.published_at_ms,
                saved_at_ms=captured.saved_at_ms,
                duration_ms=captured.duration_ms,
                availability=captured.availability,
            )
            self.session.add(source)
            self.session.flush()
            stats.sources_created += 1
            return source, True

        # Compare against the newest snapshot; identical payloads are a no-op.
        latest = self.session.scalars(
            select(SourceSnapshot)
            .where(SourceSnapshot.source_id == existing.id)
            .order_by(SourceSnapshot.fetched_at_ms.desc())
        ).first()

        existing.last_seen_at_ms = now_ms()
        if latest is not None and latest.content_hash == digest:
            stats.sources_unchanged += 1
            return existing, False

        existing.title = captured.title
        existing.caption_raw = captured.caption_raw
        existing.source_url = captured.source_url or existing.source_url
        existing.cover_url = captured.cover_url or existing.cover_url
        existing.availability = captured.availability
        existing.duration_ms = captured.duration_ms or existing.duration_ms
        if creator is not None:
            existing.creator_id = creator.id
        stats.sources_updated += 1
        return existing, True

    def _write_snapshot(
        self, source: Source, captured: CapturedSource, stats: SyncStats
    ) -> None:
        snapshot = SourceSnapshot(
            source_id=source.id,
            content_hash=self._payload_hash(captured),
            raw_json=captured.model_dump(mode="json"),
        )
        self.session.add(snapshot)
        self.session.flush()
        source.latest_snapshot_id = snapshot.id
        stats.snapshots += 1

    def _sync_assets(
        self, source: Source, captured: CapturedSource, stats: SyncStats
    ) -> None:
        """Record media as assets; keep inline subtitle text as evidence.

        `SourceAsset` has no text column by design — it describes a *file*. A
        provider-supplied subtitle is already observed text, so it is stored as a
        level-2 `EvidenceUnit` directly. This is why the demo reaches level 2
        without ffmpeg or an ASR key.
        """
        # Track which asset types are present in this capture
        current_kinds: set[str] = set()

        for media in captured.media:
            if media.kind == "subtitle" and media.text:
                if self._store_subtitle_evidence(source, media.text, media.language):
                    stats.subtitles_captured += 1
                continue

            if not media.url:
                # No URL and no inline text: there is nothing to record and nothing to
                # fetch. A row here would be a permanent `unavailable` placeholder that
                # every acquisition pass has to re-examine and reject.
                continue

            current_kinds.add(media.kind)
            fingerprint = url_fingerprint(media.url)
            existing = self.session.scalars(
                select(SourceAsset).where(
                    SourceAsset.source_id == source.id,
                    SourceAsset.asset_type == media.kind,
                    SourceAsset.remote_url_fingerprint == fingerprint,
                )
            ).first()
            if existing is not None:
                self._refresh_asset_url(existing, media.url)
                continue

            self._replace_stale_assets(source, media.kind, fingerprint, stats)
            self.session.add(
                SourceAsset(
                    source_id=source.id,
                    asset_type=media.kind,
                    # 'cache' means: safe to delete and re-fetch. Nothing here is
                    # irreplaceable, because the URLs are provider-signed anyway.
                    retention_class="cache",
                    storage_key=self.media_store.build_storage_key(
                        platform=source.platform,
                        external_id=source.external_id,
                        asset_type=media.kind,
                        url=media.url,
                        mime_type=media.mime_type,
                    ),
                    remote_url=media.url,
                    remote_url_fingerprint=fingerprint,
                    # Recorded, not fetched. Acquisition is a separate job so a sync of a
                    # thousand items does not block on a thousand downloads, and so the
                    # processing policy gets to veto the cost first (DEC-014).
                    download_state=SourceAsset.DOWNLOAD_PENDING,
                    mime_type=media.mime_type,
                    byte_size=media.byte_size,
                    duration_ms=media.duration_ms,
                    width=media.width,
                    height=media.height,
                )
            )
            stats.assets += 1

        # Retire assets whose type disappeared entirely from the new capture
        self._retire_disappeared_assets(source, current_kinds, stats)

    def _refresh_asset_url(self, asset: SourceAsset, url: str) -> None:
        """Update the signed URL without disturbing local state.

        The fingerprint matched, so this is the same remote file re-signed. Overwriting
        `download_state` here would discard a good local copy on every sync and make
        ASR re-run forever; only `remote_url` is allowed to move.
        """
        if asset.remote_url != url:
            asset.remote_url = url
            self.session.flush()

    def _replace_stale_assets(
        self, source: Source, kind: str, fingerprint: str, stats: SyncStats
    ) -> None:
        """Retire same-kind assets whose remote file is no longer the current one.

        A different fingerprint for the same source and kind means the provider is now
        pointing at different content -- re-uploaded, re-encoded, or replaced. The old
        local file is stale: keeping it `ready` would let ASR transcribe the superseded
        video and attach that evidence to the current source. So the bytes go and the
        row is marked `unavailable`, which is terminal and keeps it out of the
        acquisition queue while preserving the historical record that it existed.

        Legacy rows with a NULL fingerprint (pre-DEC-014) are retired too: their
        `storage_key` was a URL, so there is no trustworthy local file behind them.
        """
        stale = self.session.scalars(
            select(SourceAsset).where(
                SourceAsset.source_id == source.id,
                SourceAsset.asset_type == kind,
                SourceAsset.remote_url_fingerprint.is_distinct_from(fingerprint),
            )
        ).all()
        for asset in stale:
            if asset.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE:
                continue
            self.media_store.delete(asset.storage_key)
            asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
            asset.download_error = "superseded by a newer asset for this source"
            stats.assets_superseded += 1
        if stale:
            self.session.flush()

    def _retire_disappeared_assets(
        self, source: Source, current_kinds: set[str], stats: SyncStats
    ) -> None:
        """Retire assets whose type is no longer present in the authoritative capture.

        When a source initially has video A (downloaded and ready), but a later
        authoritative capture contains no video at all, the old asset must not remain
        eligible as current ASR input. This handles complete media disappearance, not
        just replacement (video A → video B), which `_replace_stale_assets` covers.
        """
        disappeared = self.session.scalars(
            select(SourceAsset).where(
                SourceAsset.source_id == source.id,
                SourceAsset.asset_type.notin_(current_kinds) if current_kinds else True,
                SourceAsset.download_state != SourceAsset.DOWNLOAD_UNAVAILABLE,
            )
        ).all()
        for asset in disappeared:
            self.media_store.delete(asset.storage_key)
            asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
            asset.download_error = "media type disappeared from authoritative capture"
            stats.assets_superseded += 1
        if disappeared:
            self.session.flush()

    def _store_subtitle_evidence(
        self, source: Source, text: str, language: str | None
    ) -> bool:
        """Persist an inline subtitle as evidence, deduplicated by content hash."""
        digest = content_hash("subtitle", text)
        existing = self.session.scalars(
            select(EvidenceUnit).where(
                EvidenceUnit.source_id == source.id,
                EvidenceUnit.kind == "subtitle",
                EvidenceUnit.content_hash == digest,
            )
        ).first()
        if existing is not None:
            return False

        self.session.add(
            EvidenceUnit(
                source_id=source.id,
                kind="subtitle",
                raw_text=text,
                normalized_text=text,
                content_hash=digest,
                language=language or "zh",
                # Provider-supplied subtitles are authored, not inferred.
                confidence=1.0,
                observation_json={"origin": "provider_native_subtitle"},
            )
        )
        return True

    def _touch_membership(self, source: Source, collection: Collection) -> None:
        membership = self.session.get(
            SourceCollectionMembership, {"source_id": source.id, "collection_id": collection.id}
        )
        if membership is None:
            self.session.add(
                SourceCollectionMembership(
                    source_id=source.id, collection_id=collection.id, is_present=1
                )
            )
            return
        membership.last_seen_at_ms = now_ms()
        membership.is_present = 1  # re-adding a removed item revives it

    def _mark_absent(self, collection: Collection, present_ids: Iterable[str]) -> int:
        keep = set(present_ids)
        rows = self.session.scalars(
            select(SourceCollectionMembership).where(
                SourceCollectionMembership.collection_id == collection.id,
                SourceCollectionMembership.is_present == 1,
            )
        ).all()
        removed = 0
        for row in rows:
            if row.source_id not in keep:
                row.is_present = 0
                removed += 1
        return removed

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _payload_hash(captured: CapturedSource) -> str:
        """Hash the payload with stable key ordering.

        Sorting keys matters: without it, a provider reordering its JSON would
        look like a content change and create a spurious snapshot on every sync.
        """
        payload = captured.model_dump(mode="json")
        return content_hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    @staticmethod
    def _raw_of(model: Any) -> dict[str, Any] | None:
        dump = getattr(model, "model_dump", None)
        return dump(mode="json") if callable(dump) else None


__all__ = ["CaptureSyncService", "SyncStats"]
