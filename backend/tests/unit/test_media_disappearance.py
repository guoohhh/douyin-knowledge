"""Media disappearance: when authoritative capture no longer has media, retire stale assets.

Current sync correctly handles:
  video A -> video B (different fingerprint, A marked unavailable)

But must also handle:
  video A -> no video in new capture (A must be marked unavailable)

Otherwise, stale A remains eligible as current ASR input.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCreator, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.media.store import MediaStore


def test_media_disappearance_retires_stale_asset(session: Session, tmp_path) -> None:
    """When new authoritative capture has no media, old ready asset must not remain eligible."""
    store = MediaStore(str(tmp_path))
    service = CaptureSyncService(session, media_store=store)

    # Initial capture with video
    initial = CapturedSource(
        platform="douyin",
        external_id="7001",
        source_url="https://example.com/7001",
        title="Has video",
        creator=CapturedCreator(external_creator_id="test", display_name="test"),
        media=[
            CapturedMedia(
                kind="video",
                url="https://cdn.example.com/video_a.mp4",
                mime_type="video/mp4",
            )
        ],
    )
    service.sync_sources([initial])
    session.commit()

    # Verify asset exists and is pending
    source = session.scalars(select(Source).where(Source.external_id == "7001")).one()
    asset_a = session.scalars(
        select(SourceAsset).where(
            SourceAsset.source_id == source.id, SourceAsset.asset_type == "video"
        )
    ).first()
    assert asset_a is not None
    assert asset_a.download_state == SourceAsset.DOWNLOAD_PENDING

    # Simulate successful download
    asset_a.download_state = SourceAsset.DOWNLOAD_READY
    asset_a.downloaded_at_ms = 1000
    session.commit()

    # Later authoritative capture for the same source - no video this time
    later = CapturedSource(
        platform="douyin",
        external_id="7001",
        source_url="https://example.com/7001",
        title="Has video",  # title unchanged
        creator=CapturedCreator(external_creator_id="test", display_name="test"),
        media=[],  # No media - video disappeared
    )
    service.sync_sources([later])
    session.commit()

    # Verify old asset A is marked unavailable, not left as ready
    session.expire_all()
    asset_a_after = session.get(SourceAsset, asset_a.id)
    assert asset_a_after is not None
    assert (
        asset_a_after.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
    ), "stale asset must not remain ready when media disappeared from new capture"
    assert "disappeared" in asset_a_after.download_error.lower() or "superseded" in asset_a_after.download_error.lower()
