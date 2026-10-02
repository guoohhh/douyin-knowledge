"""The defect P0-2 exists to fix, asserted end to end.

Every other media test checks a layer in isolation, and the demo suite cannot catch this
class of bug at all: fixtures carry an inline ``native_subtitle``, so demo mode reaches
level 2 without ever asking for media. That is precisely why ``storage_key`` could hold an
absolute signed URL for as long as it did -- the orchestrator resolved ``media_dir /
storage_key``, got a path that cannot exist, recorded ``asr_skipped_media_not_downloaded``,
and a missing transcript is not a run failure, so nothing went red.

So this file uses the shape only the real provider produces -- a video with no inline
subtitle -- and asserts the whole chain: processing notices it lacks bytes, acquisition
fetches them, and a second processing pass reaches level 2 through ASR.
"""

from __future__ import annotations

import hashlib
from typing import NoReturn

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.ai.adapters.mock_adapter import MockASRProvider, MockStructuredModel
from douyin_knowledge.ai.providers import ASRResponse, TranscriptSegment
from douyin_knowledge.capture.models import CapturedCollection, CapturedMedia, CapturedSource
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.db.models.ops import JobEvent
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator
from douyin_knowledge.jobs import handlers as handlers_module
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.registry import JobContext
from douyin_knowledge.jobs.types import JobType
from douyin_knowledge.media.downloader import MediaDownloader
from douyin_knowledge.media.service import MediaAcquisitionService
from douyin_knowledge.media.store import MediaStore

VIDEO = "https://v26.douyinvod.com/hashA/video.mp4?sign=first"
PAYLOAD = b"\x00\x01fake-mp4-bytes" * 64


class CannedASRProvider:
    """Return one exact provider response so outcome semantics are testable."""

    def __init__(self, response: ASRResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> ASRResponse:
        self.calls.append(audio_path)
        return self.response


class FailingASRProvider:
    """Raise as a provider/transport failure rather than return an empty result."""

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        timestamp_granularity: str = "segment",
    ) -> NoReturn:
        raise RuntimeError("provider unavailable")


@pytest.fixture
def source(session: Session, settings: Settings) -> Source:
    """A video source with no inline subtitle -- the real-provider shape."""
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
                caption_raw="在中环",
                media=[CapturedMedia(kind="video", url=VIDEO)],
            )
        ],
        collection=coll,
    )
    found = session.scalars(select(Source)).first()
    assert found is not None
    return found


def make_orchestrator(settings: Settings, asr: MockASRProvider) -> ProcessingOrchestrator:
    return ProcessingOrchestrator(
        settings, structured_model=MockStructuredModel(), asr_provider=asr
    )


def acquire(session: Session, settings: Settings, source_id: str) -> None:
    store = MediaStore(settings.media_dir)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=PAYLOAD))
    downloader = MediaDownloader(store, client=httpx.Client(transport=transport))
    MediaAcquisitionService(session, store, downloader).acquire_for_source(source_id)


def test_processing_reports_the_gap_before_acquisition(
    session: Session, settings: Settings, source: Source
) -> None:
    """The pre-fix silent failure is now an explicit, distinguishable note.

    ``not_downloaded`` rather than ``unavailable`` is what tells the queue this is worth
    an acquisition attempt, so the distinction is load-bearing, not cosmetic.
    """
    asr = MockASRProvider()
    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)

    assert "asr_skipped_media_not_downloaded" in outcome.notes
    assert asr.calls == [], "ASR must not be handed a path that does not exist"
    assert outcome.achieved_level < 2


def test_acquisition_then_processing_reaches_level_two_via_asr(
    session: Session, settings: Settings, source: Source
) -> None:
    """The fix, stated as the behaviour the user gets: a transcript from a video."""
    acquire(session, settings, source.id)

    asr = MockASRProvider()
    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)

    assert outcome.achieved_level >= 2
    assert "asr_skipped_media_not_downloaded" not in outcome.notes
    assert len(asr.calls) == 1, "ASR should have been invoked exactly once"

    # The path handed to ASR must be a real local file, which is the actual bug: before
    # the split, this was `media_dir` joined with an https URL.
    handed = asr.calls[0]
    assert handed.endswith(".mp4")
    assert "://" not in handed
    with open(handed, "rb") as fh:
        assert fh.read() == PAYLOAD

    transcript = session.scalars(
        select(EvidenceUnit).where(
            EvidenceUnit.source_id == source.id, EvidenceUnit.kind == "asr"
        )
    ).all()
    assert transcript, "the transcript must be persisted as evidence, not just returned"


def test_asset_row_reflects_the_downloaded_bytes(
    session: Session, settings: Settings, source: Source
) -> None:
    """`download_state` is the answer to a question `storage_key` cannot answer."""
    asset = session.scalars(select(SourceAsset)).one()
    assert asset.download_state == SourceAsset.DOWNLOAD_PENDING
    assert asset.remote_url == VIDEO
    assert "://" not in asset.storage_key, "storage_key is a relative local path (AGENTS s14)"

    acquire(session, settings, source.id)
    session.refresh(asset)

    assert asset.download_state == SourceAsset.DOWNLOAD_READY
    assert asset.byte_size == len(PAYLOAD)
    assert asset.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert asset.downloaded_at_ms is not None
    assert asset.download_error is None


def test_reprocessing_does_not_download_again(
    session: Session, settings: Settings, source: Source
) -> None:
    """Transcription is expensive enough; re-fetching the bytes each pass is waste.

    GPT's adapter downloaded into a TemporaryDirectory inside the transcribe call, so
    every retry re-fetched. Persisting under a deterministic key is what makes this
    assertion possible.
    """
    acquire(session, settings, source.id)

    calls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    store = MediaStore(settings.media_dir)
    downloader = MediaDownloader(store, client=httpx.Client(transport=httpx.MockTransport(record)))
    MediaAcquisitionService(session, store, downloader).acquire_for_source(source.id)

    assert calls == [], "the bytes are already on disk under a deterministic key"

    asr = MockASRProvider()
    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)
    assert outcome.achieved_level >= 2


def test_successful_empty_asr_is_observable_without_claiming_level_two(
    session: Session, settings: Settings, source: Source
) -> None:
    acquire(session, settings, source.id)
    asr = CannedASRProvider(
        ASRResponse(segments=[], full_text=" \n\t ", language="zh", model="test-empty")
    )

    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)

    assert outcome.status == "succeeded"
    assert outcome.target_level == 2
    assert outcome.achieved_level == 1
    assert "asr_empty_result" in outcome.notes
    assert "asr_failed" not in outcome.notes
    assert session.scalars(
        select(EvidenceUnit).where(
            EvidenceUnit.source_id == source.id,
            EvidenceUnit.kind == "asr",
        )
    ).all() == []


def test_successful_asr_with_segments_still_reaches_level_two(
    session: Session, settings: Settings, source: Source
) -> None:
    acquire(session, settings, source.id)
    asr = CannedASRProvider(
        ASRResponse(
            segments=[TranscriptSegment(text="有效转写", start_ms=100, end_ms=900)],
            full_text="有效转写",
            language="zh",
            model="test-success",
        )
    )

    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)

    assert outcome.achieved_level == 2
    assert "asr_empty_result" not in outcome.notes
    assert session.scalars(
        select(EvidenceUnit).where(
            EvidenceUnit.source_id == source.id,
            EvidenceUnit.kind == "asr",
        )
    ).one()


def test_asr_exception_remains_failed_not_empty(
    session: Session, settings: Settings, source: Source
) -> None:
    acquire(session, settings, source.id)
    orchestrator = ProcessingOrchestrator(
        settings,
        structured_model=MockStructuredModel(),
        asr_provider=FailingASRProvider(),
    )

    outcome = orchestrator.process_source(session, source.id, target_level=2)

    assert outcome.status == "succeeded"
    assert outcome.achieved_level == 1
    assert "asr_failed" in outcome.notes
    assert "asr_empty_result" not in outcome.notes


def test_nonempty_full_text_without_usable_segments_is_not_empty_result(
    session: Session, settings: Settings, source: Source
) -> None:
    acquire(session, settings, source.id)
    asr = CannedASRProvider(
        ASRResponse(
            segments=[TranscriptSegment(text=" \n", start_ms=0, end_ms=500)],
            full_text="provider returned text without timestamps",
            language="zh",
            model="test-no-segments",
        )
    )

    outcome = make_orchestrator(settings, asr).process_source(session, source.id, target_level=2)

    assert outcome.achieved_level == 1
    assert "asr_empty_result" not in outcome.notes
    assert "asr_no_usable_segments" in outcome.notes


def test_processed_job_event_persists_empty_asr_notes_and_run_id(
    session: Session,
    settings: Settings,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquire(session, settings, source.id)
    asr = CannedASRProvider(
        ASRResponse(segments=[], full_text="", language="zh", model="test-empty")
    )
    orchestrator = make_orchestrator(settings, asr)
    monkeypatch.setattr(handlers_module, "_orchestrator", lambda _settings: orchestrator)

    queue = JobQueue(session)
    job = queue.enqueue(
        JobType.PROCESS_SOURCE,
        source_id=source.id,
        payload={"source_id": source.id, "target_level": 2},
    ).job
    ctx = JobContext(job=job, session=session, queue=queue, settings=settings)

    handlers_module.handle_process_source(ctx)

    run = session.scalars(select(ProcessingRun).where(ProcessingRun.source_id == source.id)).one()
    event = session.scalars(
        select(JobEvent).where(
            JobEvent.job_id == job.id,
            JobEvent.event_type == "processed",
        )
    ).one()
    assert event.data_json is not None
    assert event.data_json["run_id"] == run.id
    assert "asr_empty_result" in event.data_json["notes"]
