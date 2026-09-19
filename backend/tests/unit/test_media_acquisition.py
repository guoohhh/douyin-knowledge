"""Acquisition state machine and its interaction with sync.

The bug this whole area exists to fix was invisible because demo mode never needs ASR:
the fixtures carry inline subtitles, so the real-provider path could be broken for as
long as it liked while the suite stayed green. These tests therefore use *video* assets
with no inline subtitle, which is the shape only the real provider produces.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCollection, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import MediaDownloadFailed
from douyin_knowledge.db.models.capture import SourceAsset
from douyin_knowledge.media.downloader import MediaDownloader
from douyin_knowledge.media.service import MediaAcquisitionService
from douyin_knowledge.media.store import MediaStore

VIDEO_A = "https://v26.douyinvod.com/hashA/video.mp4?sign=aaa&expire=1"
VIDEO_A_RESIGNED = "https://v26.douyinvod.com/hashA/video.mp4?sign=bbb&expire=2"
VIDEO_B = "https://v26.douyinvod.com/hashB/video.mp4?sign=ccc&expire=3"
AUDIO_A = "https://v26.douyinvod.com/hashA/audio.m4a?sign=ddd"


def captured(
    external_id: str = "7100", *, media: list[CapturedMedia] | None = None
) -> CapturedSource:
    return CapturedSource(
        platform="douyin",
        external_id=external_id,
        title="一家很好的茶餐厅",
        caption_raw="",
        media=media if media is not None else [CapturedMedia(kind="video", url=VIDEO_A)],
    )


def collection() -> CapturedCollection:
    return CapturedCollection(external_collection_id="col1", name="收藏夹")


def sync_one(session: Session, store: MediaStore, source: CapturedSource) -> SourceAsset | None:
    service = CaptureSyncService(session, media_store=store)
    coll = service.upsert_collection(collection())
    service.sync_sources([source], collection=coll)
    return session.scalars(
        select(SourceAsset).where(SourceAsset.asset_type == "video")
    ).first()


def responder(payload: bytes = b"video-bytes", status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"content-type": "video/mp4"})

    return handler


def service_for(
    session: Session, store: MediaStore, handler: object, *, max_attempts: int = 3
) -> MediaAcquisitionService:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return MediaAcquisitionService(
        session, store, MediaDownloader(store, client=client), max_attempts=max_attempts
    )


@pytest.fixture
def store(settings: Settings) -> MediaStore:
    return MediaStore(settings.media_dir)


class TestSyncWritesTheRightColumns:
    def test_storage_key_is_local_and_relative_not_a_url(
        self, session: Session, store: MediaStore
    ) -> None:
        """The exact defect: a URL in storage_key made `media_dir / key` unreachable."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        assert asset.storage_key is not None
        assert not asset.storage_key.startswith("http")
        assert not Path(asset.storage_key).is_absolute()
        assert asset.remote_url == VIDEO_A
        assert asset.download_state == SourceAsset.DOWNLOAD_PENDING

    def test_sync_does_not_download(self, session: Session, store: MediaStore) -> None:
        """Acquisition is a separate job so policy can veto the cost first."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        assert store.exists(asset.storage_key) is False
        assert asset.downloaded_at_ms is None

    def test_media_without_url_creates_no_asset(
        self, session: Session, store: MediaStore
    ) -> None:
        asset = sync_one(
            session, store, captured(media=[CapturedMedia(kind="video", url=None)])
        )
        assert asset is None

    def test_inline_subtitle_still_bypasses_media_entirely(
        self, session: Session, store: MediaStore
    ) -> None:
        """Demo mode's path must keep working with no download and no ffmpeg."""
        service = CaptureSyncService(session, media_store=store)
        coll = service.upsert_collection(collection())
        stats = service.sync_sources(
            [
                captured(
                    media=[
                        CapturedMedia(kind="subtitle", text="这家店人均八十", language="zh")
                    ]
                )
            ],
            collection=coll,
        )
        assert stats.subtitles_captured == 1
        assert stats.assets == 0


class TestResigningVersusRealChange:
    def test_resigned_url_refreshes_without_touching_local_state(
        self, session: Session, store: MediaStore
    ) -> None:
        """Douyin re-signs on every capture. If that counted as a change, every sync
        would discard a good local file and re-download it forever."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        key, asset_id = asset.storage_key, asset.id
        store.ensure_parent(key).write_bytes(b"already-downloaded")
        asset.download_state = SourceAsset.DOWNLOAD_READY
        asset.downloaded_at_ms = 111
        session.flush()

        sync_one(session, store, captured(media=[CapturedMedia(kind="video", url=VIDEO_A_RESIGNED)]))

        refreshed = session.get(SourceAsset, asset_id)
        assert refreshed is not None
        assert refreshed.remote_url == VIDEO_A_RESIGNED  # provenance updated
        assert refreshed.storage_key == key  # destination unchanged
        assert refreshed.download_state == SourceAsset.DOWNLOAD_READY
        assert refreshed.downloaded_at_ms == 111
        assert store.exists(key) is True

    def test_resigning_creates_no_second_asset(
        self, session: Session, store: MediaStore
    ) -> None:
        sync_one(session, store, captured())
        sync_one(session, store, captured(media=[CapturedMedia(kind="video", url=VIDEO_A_RESIGNED)]))
        assets = session.scalars(select(SourceAsset)).all()
        assert len(assets) == 1

    def test_changed_content_retires_the_stale_file(
        self, session: Session, store: MediaStore
    ) -> None:
        """A replaced upload must not leave the old video `ready`, or ASR would
        transcribe superseded content and attach it to the current source."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        old_key, old_id = asset.storage_key, asset.id
        store.ensure_parent(old_key).write_bytes(b"old-video")
        asset.download_state = SourceAsset.DOWNLOAD_READY
        session.flush()

        sync_one(session, store, captured(media=[CapturedMedia(kind="video", url=VIDEO_B)]))

        old = session.get(SourceAsset, old_id)
        assert old is not None
        assert old.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
        assert store.exists(old_key) is False  # bytes actually removed
        # History is kept: the row still records that this asset existed.
        assert old.download_error is not None

        current = session.scalars(
            select(SourceAsset).where(SourceAsset.remote_url == VIDEO_B)
        ).first()
        assert current is not None
        assert current.download_state == SourceAsset.DOWNLOAD_PENDING
        assert current.storage_key != old_key


class TestAcquire:
    def test_downloads_and_records_state(self, session: Session, store: MediaStore) -> None:
        asset = sync_one(session, store, captured())
        assert asset is not None
        result = service_for(session, store, responder()).acquire(asset)

        assert result.ready is True
        assert result.outcome == "downloaded"
        assert asset.download_state == SourceAsset.DOWNLOAD_READY
        assert asset.downloaded_at_ms is not None
        assert asset.sha256 is not None
        assert asset.byte_size == len(b"video-bytes")
        assert store.exists(asset.storage_key) is True

    def test_is_idempotent_against_the_disk(self, session: Session, store: MediaStore) -> None:
        """A reclaimed job whose predecessor actually finished must not re-download."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        calls = 0

        def counting(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"video-bytes")

        service = service_for(session, store, counting)
        service.acquire(asset)
        second = service.acquire(asset)

        assert calls == 1
        assert second.outcome == "already_present"

    def test_present_file_repairs_a_stale_pending_row(
        self, session: Session, store: MediaStore
    ) -> None:
        """Bytes on disk are the truth; a row that disagrees is corrected, not obeyed."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        store.ensure_parent(asset.storage_key).write_bytes(b"arrived-out-of-band")

        def refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not download when the file is already there")

        result = service_for(session, store, refuse).acquire(asset)
        assert result.outcome == "already_present"
        assert asset.download_state == SourceAsset.DOWNLOAD_READY

    def test_retryable_failure_records_the_attempt_and_reraises(
        self, session: Session, store: MediaStore
    ) -> None:
        """The row must explain itself even if the job is never retried."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        with pytest.raises(MediaDownloadFailed):
            service_for(session, store, responder(status=503)).acquire(asset)

        assert asset.download_state == SourceAsset.DOWNLOAD_FAILED
        assert asset.download_attempts == 1
        assert asset.download_error is not None

    def test_permanent_failure_is_terminal_not_retried(
        self, session: Session, store: MediaStore
    ) -> None:
        asset = sync_one(session, store, captured())
        assert asset is not None
        result = service_for(session, store, responder(status=404)).acquire(asset)

        assert result.outcome == "unavailable"
        assert asset.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE
        # And it is now excluded from future work rather than retried forever.
        service = service_for(session, store, responder(status=404))
        assert service.pending_assets(asset.source_id) == []

    def test_asset_without_remote_url_is_unavailable(
        self, session: Session, store: MediaStore
    ) -> None:
        asset = sync_one(session, store, captured())
        assert asset is not None
        asset.remote_url = None
        session.flush()
        result = service_for(session, store, responder()).acquire(asset)
        assert result.state == SourceAsset.DOWNLOAD_UNAVAILABLE

    def test_retry_cap_stops_pending_selection(
        self, session: Session, store: MediaStore
    ) -> None:
        asset = sync_one(session, store, captured())
        assert asset is not None
        service = service_for(session, store, responder(status=503), max_attempts=2)
        for _ in range(2):
            with pytest.raises(MediaDownloadFailed):
                service.acquire(asset)
        assert asset.download_attempts == 2
        assert service.pending_assets(asset.source_id) == []


class TestSelection:
    def test_audio_is_preferred_over_video(self, session: Session, store: MediaStore) -> None:
        """Same content, a fraction of the bytes."""
        sync_one(
            session,
            store,
            captured(
                media=[
                    CapturedMedia(kind="video", url=VIDEO_A),
                    CapturedMedia(kind="audio", url=AUDIO_A),
                ]
            ),
        )
        service = service_for(session, store, responder())
        source_id = session.scalars(select(SourceAsset.source_id)).first()
        assert source_id is not None
        ordered = [a.asset_type for a in service.transcribable_assets(source_id)]
        assert ordered == ["audio", "video"]

    def test_acquire_for_source_stops_after_one_success(
        self, session: Session, store: MediaStore
    ) -> None:
        sync_one(
            session,
            store,
            captured(
                media=[
                    CapturedMedia(kind="video", url=VIDEO_A),
                    CapturedMedia(kind="audio", url=AUDIO_A),
                ]
            ),
        )
        source_id = session.scalars(select(SourceAsset.source_id)).first()
        assert source_id is not None
        calls = 0

        def counting(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"bytes")

        results = service_for(session, store, counting).acquire_for_source(source_id)
        assert calls == 1
        assert len(results) == 1
        assert results[0].ready is True

    def test_local_asset_for_asr_ignores_a_ready_row_with_no_file(
        self, session: Session, store: MediaStore
    ) -> None:
        """A `ready` row whose file was deleted by cache cleanup must not be handed to
        ASR as a path that does not exist."""
        asset = sync_one(session, store, captured())
        assert asset is not None
        asset.download_state = SourceAsset.DOWNLOAD_READY
        session.flush()
        service = service_for(session, store, responder())
        assert service.local_asset_for_asr(asset.source_id) is None
        # ...and it becomes eligible for re-acquisition rather than being stuck.
        assert service.pending_assets(asset.source_id) == [asset]
