"""The acquisition job: policy gates the spend, and the chain terminates.

Two requirements from the integration brief are only observable at this layer:
"expensive acquisition only after Processing Policy allows" and "failures persisted and
observable". Both are about the *job*, not the downloader.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import CapturedCollection, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import MediaDownloadFailed
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.db.models.ops import JobEvent
from douyin_knowledge.jobs import handlers as handlers_module
from douyin_knowledge.jobs.handlers import handle_acquire_media
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.registry import JobContext
from douyin_knowledge.jobs.types import JobType
from douyin_knowledge.media.downloader import MediaDownloader
from douyin_knowledge.media.service import MediaAcquisitionService
from douyin_knowledge.media.store import MediaStore
from douyin_knowledge.policy.models import PolicyAction, PolicyDecision

VIDEO = "https://v26.douyinvod.com/hashA/video.mp4?sign=aaa"


@pytest.fixture
def seeded(session: Session, settings: Settings) -> Source:
    service = CaptureSyncService(session, media_store=MediaStore(settings.media_dir))
    coll = service.upsert_collection(
        CapturedCollection(external_collection_id="col1", name="收藏夹")
    )
    service.sync_sources(
        [
            CapturedSource(
                platform="douyin",
                external_id="7100",
                title="一家茶餐厅",
                media=[CapturedMedia(kind="video", url=VIDEO)],
            )
        ],
        collection=coll,
    )
    source = session.scalars(select(Source)).first()
    assert source is not None
    return source


def make_ctx(session: Session, settings: Settings, source: Source) -> JobContext:
    queue = JobQueue(session)
    job = queue.enqueue(
        JobType.ACQUIRE_MEDIA,
        source_id=source.id,
        payload={"source_id": source.id, "target_level": 2},
    ).job
    return JobContext(job=job, session=session, queue=queue, settings=settings)


def events_of(session: Session, ctx: JobContext) -> list[str]:
    return [
        e.event_type
        for e in session.scalars(select(JobEvent).where(JobEvent.job_id == ctx.job.id)).all()
    ]


def patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: object, *, calls: list[str] | None = None
) -> None:
    """Route the handler's downloader through MockTransport.

    The handler builds its own service from settings, which is the behaviour under test,
    so the seam is the service factory rather than a constructor argument.
    """
    real = handlers_module._acquisition_service

    def factory(ctx: JobContext) -> MediaAcquisitionService:
        service = real(ctx)
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        service.downloader = MediaDownloader(service.store, client=client)
        return service

    monkeypatch.setattr(handlers_module, "_acquisition_service", factory)


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"video-bytes")


class TestPolicyGate:
    def test_excluded_source_is_never_downloaded(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The most expensive step in the system must respect a rule added after the
        job was queued. Re-checked in the handler, not trusted from the enqueuer."""

        def refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("policy said no; nothing may be fetched")

        patch_transport(monkeypatch, refuse)
        monkeypatch.setattr(
            handlers_module,
            "_apply_policy",
            lambda ctx, source_id: (
                False,
                PolicyDecision(
                    id="dec_1",
                    source_id=source_id,
                    rule_id="rule_1",
                    phase="metadata",
                    action=PolicyAction.EXCLUDE,
                    reason_code="creator_excluded",
                ),
            ),
        )
        ctx = make_ctx(session, settings, seeded)
        handle_acquire_media(ctx)

        assert "media_skipped_by_policy" in events_of(session, ctx)
        asset = session.scalars(select(SourceAsset)).first()
        assert asset is not None
        assert asset.download_state == SourceAsset.DOWNLOAD_PENDING
        assert asset.download_attempts == 0

    def test_allowed_source_is_downloaded(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_transport(monkeypatch, ok)
        ctx = make_ctx(session, settings, seeded)
        handle_acquire_media(ctx)

        asset = session.scalars(select(SourceAsset)).first()
        assert asset is not None
        assert asset.download_state == SourceAsset.DOWNLOAD_READY
        assert "media_acquired" in events_of(session, ctx)


class TestChaining:
    def test_success_resumes_processing(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_transport(monkeypatch, ok)
        ctx = make_ctx(session, settings, seeded)
        handle_acquire_media(ctx)

        queued = [j.job_type for j in ctx.queue.pending_for_source(seeded.id)]
        assert str(JobType.PROCESS_SOURCE) in queued

    def test_permanent_failure_does_not_requeue_processing(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing to transcribe and nothing coming. Re-running the ladder would reach
        the identical conclusion, so the chain stops rather than looping."""

        def gone(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        patch_transport(monkeypatch, gone)
        ctx = make_ctx(session, settings, seeded)
        handle_acquire_media(ctx)

        queued = [j.job_type for j in ctx.queue.pending_for_source(seeded.id)]
        assert str(JobType.PROCESS_SOURCE) not in queued
        asset = session.scalars(select(SourceAsset)).first()
        assert asset is not None
        assert asset.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE

    def test_nothing_pending_is_reported_not_silent(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        patch_transport(monkeypatch, ok)
        asset = session.scalars(select(SourceAsset)).first()
        assert asset is not None
        asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
        session.flush()

        ctx = make_ctx(session, settings, seeded)
        handle_acquire_media(ctx)
        assert "media_nothing_to_acquire" in events_of(session, ctx)

    def test_retryable_failure_propagates_for_queue_retry(
        self,
        session: Session,
        settings: Settings,
        seeded: Source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The handler must not swallow this: the queue owns the retry policy, and a
        swallowed failure would look like a successful no-op."""

        def flaky(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        patch_transport(monkeypatch, flaky)
        ctx = make_ctx(session, settings, seeded)
        with pytest.raises(MediaDownloadFailed) as excinfo:
            handle_acquire_media(ctx)
        assert excinfo.value.retryable is True

        asset = session.scalars(select(SourceAsset)).first()
        assert asset is not None
        assert asset.download_attempts == 1
        assert asset.download_error is not None
