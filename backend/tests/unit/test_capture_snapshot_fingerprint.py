"""Ephemeral sidecar observations must not inflate immutable source history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCreator, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.db.models.capture import Source, SourceAsset, SourceSnapshot


def captured(signature: str, *, caption: str = "原文") -> CapturedSource:
    url = f"https://media.example/video/one.mp4?signature={signature}"
    return CapturedSource(
        platform="douyin", external_id="one", title="标题", caption_raw=caption,
        cover_url=f"https://media.example/cover/one.jpg?signature={signature}",
        creator=CapturedCreator(
            external_creator_id="creator", display_name="作者",
            avatar_url=f"https://media.example/avatar/one.jpg?signature={signature}",
            raw={"fetched_at": signature},
        ),
        media=[CapturedMedia(kind="video", url=url, mime_type="video/mp4")],
        statistics={"digg_count": 20 if signature == "first" else 21},
        raw={"fetched_at": signature, "media": {"video": {"url": url}}},
    )


def test_repeated_signed_url_and_fetch_time_do_not_create_snapshots(session: Session) -> None:
    service = CaptureSyncService(session, platform="douyin")
    first = service.sync_sources([captured("first")])
    second = service.sync_sources([captured("second")])
    assert first.snapshots == 1 and second.snapshots == 0
    source = session.scalar(select(Source).where(Source.external_id == "one"))
    assert source is not None
    assert source.cover_url is not None and "second" in source.cover_url
    asset = session.scalar(select(SourceAsset).where(SourceAsset.source_id == source.id))
    assert asset is not None and asset.remote_url is not None and "second" in asset.remote_url
    snapshots = session.scalars(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id)).all()
    assert len(snapshots) == 1
    assert snapshots[0].raw_json["raw"]["fetched_at"] == "first"

    changed = service.sync_sources([captured("third", caption="修改后的原文")])
    assert changed.snapshots == 1
    assert session.query(SourceSnapshot).count() == 2
    assert session.query(Source).count() == 1
