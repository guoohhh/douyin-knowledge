"""Fixture capture provider.

This is not a mock in the "pretend to work" sense: it is a real implementation of
:class:`CaptureProvider` over an on-disk/in-module dataset, so the entire pipeline
(sync, policy, processing, wiki, retrieval, answering) runs end to end with no
credentials and no network. It is what ``DK_CAPTURE_PROVIDER=fixture`` selects and
what the test suite uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from douyin_knowledge.capture.base import CaptureProvider
from douyin_knowledge.capture.fixtures import demo_collection
from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedMedia,
    CapturedSource,
    ProviderHealth,
    SourcePage,
)
from douyin_knowledge.core.errors import NotFoundError, SourceUnavailable
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)


class FixtureCaptureProvider(CaptureProvider):
    """Serves a fixed dataset. Optionally loaded from a directory of JSON files.

    Directory layout when ``root`` is given::

        collections.json   list[CapturedCollection]
        sources.json       list[CapturedSource] with an extra "collections": [...] key
    """

    name = "fixture"

    def __init__(self, root: Path | None = None, *, platform: str = demo_collection.PLATFORM) -> None:
        self.platform = platform
        self.root = root
        self._collections: list[CapturedCollection]
        self._sources: dict[str, CapturedSource]
        self._membership: dict[str, list[str]]
        self._unavailable: set[str] = set()
        self._load()

    # ---- loading -------------------------------------------------------
    def _load(self) -> None:
        if self.root is not None:
            collections_raw = json.loads((self.root / "collections.json").read_text("utf-8"))
            sources_raw = json.loads((self.root / "sources.json").read_text("utf-8"))
            membership: dict[str, list[str]] = {}
            cleaned: list[dict[str, Any]] = []
            for item in sources_raw:
                item = dict(item)
                for col in item.pop("collections", []):
                    membership.setdefault(col, []).append(item["external_id"])
                item.setdefault("platform", self.platform)
                cleaned.append(item)
        else:
            collections_raw = demo_collection.COLLECTIONS
            cleaned = demo_collection.SOURCES
            membership = demo_collection.MEMBERSHIPS

        self._collections = [CapturedCollection.model_validate(c) for c in collections_raw]
        self._sources = {
            s["external_id"]: CapturedSource.model_validate(s) for s in cleaned
        }
        self._membership = membership

    # ---- provider interface --------------------------------------------
    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            ok=True,
            detail=f"{len(self._sources)} fixture sources in {len(self._collections)} collections",
            requires_credentials=False,
        )

    def list_collections(self) -> list[CapturedCollection]:
        out = []
        for collection in self._collections:
            ids = self._membership.get(collection.external_collection_id, [])
            out.append(collection.model_copy(update={"item_count": len(ids)}))
        return out

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        ids = self._membership.get(external_collection_id)
        if ids is None:
            raise NotFoundError(
                f"unknown fixture collection {external_collection_id!r}",
                collection_id=external_collection_id,
            )
        offset = int(cursor) if cursor else 0
        window = ids[offset : offset + limit]
        sources = [self._sources[i] for i in window if i in self._sources]
        next_offset = offset + len(window)
        has_more = next_offset < len(ids)
        return SourcePage(
            sources=sources,
            next_cursor=str(next_offset) if has_more else None,
            has_more=has_more,
        )

    def fetch_source(self, external_id: str) -> CapturedSource:
        if external_id in self._unavailable:
            raise SourceUnavailable(
                f"fixture source {external_id!r} marked unavailable", external_id=external_id
            )
        source = self._sources.get(external_id)
        if source is None:
            raise SourceUnavailable(f"no fixture source {external_id!r}", external_id=external_id)
        return source

    def download_media(self, media: CapturedMedia, destination: Path) -> Path:
        """Fixtures have no real bytes. Inline subtitle text is written verbatim;
        anything else produces a small deterministic placeholder file so that asset
        bookkeeping and retention logic are still exercised."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if media.text is not None:
            destination.write_text(media.text, encoding="utf-8")
        else:
            destination.write_bytes(b"DKFIXTURE" + (media.url or "").encode("utf-8"))
        return destination

    # ---- test affordances ----------------------------------------------
    def mark_unavailable(self, external_id: str) -> None:
        """Simulate an upstream deletion. Used by sync tests."""
        self._unavailable.add(external_id)

    def remove_from_collection(self, collection_id: str, external_id: str) -> None:
        """Simulate the user un-saving an item, to test is_present handling."""
        ids = self._membership.get(collection_id, [])
        self._membership[collection_id] = [i for i in ids if i != external_id]

    def add_source(self, payload: dict[str, Any], *, collections: list[str]) -> CapturedSource:
        source = CapturedSource.model_validate({"platform": self.platform, **payload})
        self._sources[source.external_id] = source
        for col in collections:
            self._membership.setdefault(col, []).append(source.external_id)
        return source
