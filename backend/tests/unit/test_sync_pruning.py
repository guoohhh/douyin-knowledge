"""Membership pruning is gated on a complete walk.

This is the database-level half of the pagination work. The scenario it guards is
mundane and destructive: a user has 3 pages of saved items, the sidecar fails on page
2, and the sync marks everything on pages 2 and 3 as no longer in the collection.
Nothing raises at the data layer, the rows are still there, and the only visible
symptom is that the library looks smaller than it is.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedCreator,
    CapturedSource,
    SourcePage,
)
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.core.errors import ValidationError
from douyin_knowledge.db.models.capture import Collection, Source, SourceCollectionMembership

COLLECTION = CapturedCollection(external_collection_id="col-1", name="茶餐厅")


def make_source(external_id: str) -> CapturedSource:
    return CapturedSource(
        platform="douyin",
        external_id=external_id,
        source_type="video",
        title=f"video {external_id}",
        caption_raw="",
        source_url=f"https://example.invalid/{external_id}",
        creator=CapturedCreator(external_creator_id="creator-1", display_name="Creator One"),
    )


class ScriptedProvider:
    def __init__(self, pages: list[SourcePage | Exception]) -> None:
        self.pages = pages
        self.calls = 0

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        index = self.calls
        self.calls += 1
        item = self.pages[index]
        if isinstance(item, Exception):
            raise item
        return item


def present_ids(session: Session) -> set[str]:
    """External ids whose membership is currently marked present."""
    rows = session.execute(
        select(Source.external_id)
        .join(SourceCollectionMembership, SourceCollectionMembership.source_id == Source.id)
        .where(SourceCollectionMembership.is_present == 1)
    ).all()
    return {row[0] for row in rows}


def seed_three_items(session: Session) -> None:
    """A collection fully synced across three pages, all present."""
    service = CaptureSyncService(session)
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            SourcePage(sources=[make_source("b")], next_cursor="c2", has_more=True),
            SourcePage(sources=[make_source("c")], next_cursor=None, has_more=False),
        ]
    )
    service.sync_collection(provider, COLLECTION)
    session.commit()
    assert present_ids(session) == {"a", "b", "c"}


def test_complete_walk_prunes_a_genuinely_removed_item(session: Session) -> None:
    """The positive case. Without this, "never prune" would pass every test below by
    simply never pruning at all, which would break the feature instead of fixing it."""
    seed_three_items(session)

    service = CaptureSyncService(session)
    provider = ScriptedProvider(
        [SourcePage(sources=[make_source("a"), make_source("c")], next_cursor=None, has_more=False)]
    )
    stats = service.sync_collection(provider, COLLECTION)
    session.commit()

    assert present_ids(session) == {"a", "c"}
    assert stats.memberships_removed == 1
    # Disappearance is not deletion: the Source row survives (AGENTS 4).
    assert session.scalars(select(Source).where(Source.external_id == "b")).first() is not None


def test_failure_on_an_intermediate_page_prunes_nothing(session: Session) -> None:
    seed_three_items(session)

    service = CaptureSyncService(session)
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="c1", has_more=True),
            RuntimeError("sidecar died on page 2"),
        ]
    )
    with pytest.raises(RuntimeError, match="page 2"):
        service.sync_collection(provider, COLLECTION)
    session.commit()

    # 'b' and 'c' were never seen this run. They must not be marked removed.
    assert present_ids(session) == {"a", "b", "c"}


def test_repeated_cursor_prunes_nothing(session: Session) -> None:
    seed_three_items(session)

    service = CaptureSyncService(session)
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a")], next_cursor="stuck", has_more=True),
            SourcePage(sources=[make_source("a")], next_cursor="stuck", has_more=True),
        ]
    )
    with pytest.raises(ValidationError, match="repeated a pagination cursor"):
        service.sync_collection(provider, COLLECTION)
    session.commit()

    assert present_ids(session) == {"a", "b", "c"}


def test_first_page_failure_prunes_nothing(session: Session) -> None:
    """A sidecar that is simply down must not be read as "the collection is empty"."""
    seed_three_items(session)

    service = CaptureSyncService(session)
    provider = ScriptedProvider([RuntimeError("sidecar unreachable")])
    with pytest.raises(RuntimeError):
        service.sync_collection(provider, COLLECTION)
    session.commit()

    assert present_ids(session) == {"a", "b", "c"}


def test_partial_walk_still_persists_the_pages_it_read(session: Session) -> None:
    """Partial data is kept because upserts are non-destructive and re-fetching costs
    an identity. The failure is signalled by the exception, not by discarding work."""
    service = CaptureSyncService(session)
    provider = ScriptedProvider(
        [
            SourcePage(sources=[make_source("a"), make_source("b")], next_cursor="c1", has_more=True),
            RuntimeError("sidecar died on page 2"),
        ]
    )
    with pytest.raises(RuntimeError):
        service.sync_collection(provider, COLLECTION)
    session.commit()

    stored = {row[0] for row in session.execute(select(Source.external_id)).all()}
    assert stored == {"a", "b"}


def test_partial_walk_does_not_claim_a_completed_sync(session: Session) -> None:
    """`last_synced_at_ms` is what the UI shows as "last updated". A failed walk must
    not advance it, or the user is told a sync succeeded that did not."""
    seed_three_items(session)
    collection = session.scalars(select(Collection)).one()
    synced_at = collection.last_synced_at_ms
    assert synced_at is not None

    service = CaptureSyncService(session)
    provider = ScriptedProvider([RuntimeError("sidecar unreachable")])
    with pytest.raises(RuntimeError):
        service.sync_collection(provider, COLLECTION)
    session.commit()

    session.refresh(collection)
    assert collection.last_synced_at_ms == synced_at


def test_reappearing_item_revives_its_membership(session: Session) -> None:
    """Removal is reversible: re-saving an item flips `is_present` back rather than
    leaving a tombstone the user cannot clear."""
    seed_three_items(session)

    service = CaptureSyncService(session)
    gone = ScriptedProvider(
        [SourcePage(sources=[make_source("a")], next_cursor=None, has_more=False)]
    )
    service.sync_collection(gone, COLLECTION)
    session.commit()
    assert present_ids(session) == {"a"}

    back = ScriptedProvider(
        [
            SourcePage(
                sources=[make_source("a"), make_source("b"), make_source("c")],
                next_cursor=None,
                has_more=False,
            )
        ]
    )
    service.sync_collection(back, COLLECTION)
    session.commit()

    assert present_ids(session) == {"a", "b", "c"}
