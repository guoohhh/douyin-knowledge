"""Moving a source between collections must not fork its identity.

A source that leaves folder A and appears in folder B is the *same* saved video: its
evidence, claims and processing history belong to one identity. The membership rows are
what change, and the old one is soft-removed rather than deleted, because "this used to be
in 收藏夹A" is a fact about how the user organised their library (AGENTS s4: disappearance
is not deletion).

The failure this guards against is a sync that treats the B-sighting as a new source,
which would duplicate the row and re-run extraction against identical content.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCreator, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.db.models.capture import (
    Collection,
    Source,
    SourceCollectionMembership,
)

#: Identity is (platform, external_id) -- the unique constraint on `sources`. Keeping it
#: fixed across both sightings is the whole point: the folder changed, the video did not.
EXTERNAL_ID = "movement-1"


def _captured(title: str = "一家茶餐厅") -> CapturedSource:
    return CapturedSource(
        platform="douyin",
        external_id=EXTERNAL_ID,
        source_url=f"https://www.douyin.com/video/{EXTERNAL_ID}",
        title=title,
        creator=CapturedCreator(
            external_creator_id="chef-1", handle="chef", display_name="主厨"
        ),
    )


def _collection(session: Session, external_id: str, title: str) -> Collection:
    collection = Collection(
        platform="douyin",
        external_collection_id=external_id,
        name=title,
    )
    session.add(collection)
    session.flush()
    return collection


def _memberships(session: Session, source_id: str) -> dict[str, int]:
    """collection_id -> is_present, for every membership row this source has."""
    rows = session.execute(
        select(
            SourceCollectionMembership.collection_id,
            SourceCollectionMembership.is_present,
        ).where(SourceCollectionMembership.source_id == source_id)
    ).all()
    return {collection_id: is_present for collection_id, is_present in rows}


def test_source_moved_between_collections_keeps_one_identity(session: Session) -> None:
    service = CaptureSyncService(session)
    folder_a = _collection(session, "fav-a", "收藏夹A")
    folder_b = _collection(session, "fav-b", "收藏夹B")

    # Sighting 1: present in A.
    service.sync_sources([_captured()], collection=folder_a, prune_missing=True)
    sources = session.scalars(select(Source)).all()
    assert len(sources) == 1, "first sync must create exactly one source"
    source_id = sources[0].id
    assert _memberships(session, source_id) == {folder_a.id: 1}

    # Sighting 2: gone from A. An empty authoritative walk of A must retire the membership,
    # not the source.
    service.sync_sources([], collection=folder_a, prune_missing=True)

    # Sighting 3: the same video turns up in B.
    service.sync_sources([_captured()], collection=folder_b, prune_missing=True)

    after = session.scalars(select(Source)).all()
    assert len(after) == 1, (
        "the same source_url in a different collection must not create a second source"
    )
    assert after[0].id == source_id, "source identity must be preserved across the move"
    assert after[0].locally_deleted_at_ms is None, "moving folders is not a deletion"

    memberships = _memberships(session, source_id)
    assert memberships[folder_a.id] == 0, "the A membership must be historical, not present"
    assert memberships[folder_b.id] == 1, "the B membership must be current"
    assert len(memberships) == 2, "both memberships are kept as the organisation history"


def test_returning_to_the_original_collection_revives_that_membership(
    session: Session,
) -> None:
    """Coming back to A must reuse the existing row rather than leaving it retired."""
    service = CaptureSyncService(session)
    folder_a = _collection(session, "fav-a", "收藏夹A")

    service.sync_sources([_captured()], collection=folder_a, prune_missing=True)
    source_id = session.scalars(select(Source)).one().id
    service.sync_sources([], collection=folder_a, prune_missing=True)
    assert _memberships(session, source_id) == {folder_a.id: 0}

    service.sync_sources([_captured()], collection=folder_a, prune_missing=True)

    assert _memberships(session, source_id) == {folder_a.id: 1}
    assert len(session.scalars(select(Source)).all()) == 1
