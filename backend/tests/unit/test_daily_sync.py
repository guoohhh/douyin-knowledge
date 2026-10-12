"""Daily scheduler: due slots, durable recovery, queue safety and real outcomes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.api import create_app
from douyin_knowledge.capture.daily_sync import (
    DailySyncUpdate,
    due_and_next,
    read_state,
    refresh_outcome,
    save_state,
    tick,
    view_state,
)
from douyin_knowledge.capture.scope import CaptureScope, set_capture_scope
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.errors import CaptureUnavailable
from douyin_knowledge.db import session_scope
from douyin_knowledge.db.models.capture import Collection, Source, SourceSnapshot
from douyin_knowledge.db.models.ops import Job
from douyin_knowledge.jobs.handlers import register_default_handlers
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.types import JobType
from douyin_knowledge.jobs.worker import Worker


def ms(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def enabled(session: Session, *, at: int = ms(10, 0)) -> None:
    save_state(
        session,
        DailySyncUpdate(enabled=True, local_time="03:00", timezone="UTC"),  # type: ignore[arg-type]
        at_ms=at,
    )


def test_disabled_by_default_and_persisted_settings(session: Session, settings: Settings) -> None:
    assert read_state(session).enabled is False
    assert tick(session, settings, at_ms=ms(12, 5)).last_job_id is None
    enabled(session)
    session.expire_all()
    view = view_state(session, at_ms=ms(10, 1))
    assert view.enabled and view.local_time.hour == 3 and view.timezone == "UTC"
    assert view.next_run_at_ms == ms(10, 3)
    save_state(session, DailySyncUpdate(enabled=False, local_time="03:00", timezone="UTC"))  # type: ignore[arg-type]
    assert not read_state(session).enabled


def test_trigger_catchup_and_no_duplicate_ticks(session: Session, settings: Settings) -> None:
    set_capture_scope(session, CaptureScope(named_collection_ids=["fixture-a"]))
    enabled(session)
    first = tick(session, settings, at_ms=ms(10, 3))
    assert first.last_due_at_ms == ms(10, 3)
    assert first.last_job_id is not None
    job = session.get(Job, first.last_job_id)
    assert job is not None
    assert job.job_type == JobType.SYNC_CAPTURE_SCOPE
    assert job.payload_json == {"auto_process": False, "scheduled_due_at_ms": ms(10, 3)}
    assert tick(session, settings, at_ms=ms(10, 5)).last_job_id == first.last_job_id
    assert session.query(Job).count() == 1

    # Close/sleep across two slots. Only the latest overdue slot is submitted.
    JobQueue(session).succeed(job)
    catchup = tick(session, settings, at_ms=ms(12, 5))
    assert catchup.last_due_at_ms == ms(12, 3)
    assert catchup.last_attempt_at_ms == ms(12, 5)
    assert session.query(Job).count() == 1  # existing terminal dedupe row re-armed
    assert tick(session, settings, at_ms=ms(12, 6)).last_due_at_ms == ms(12, 3)


def test_active_manual_jobs_defer_without_inheriting_paid_payload(
    session: Session, settings: Settings
) -> None:
    set_capture_scope(session, CaptureScope(named_collection_ids=["fixture-a"]))
    enabled(session)
    queue = JobQueue(session)
    manual = queue.enqueue(
        JobType.SYNC_CAPTURE_SCOPE,
        payload={"auto_process": True},
        dedupe_key="sync_capture_scope:douyin",
    ).job
    assert tick(session, settings, at_ms=ms(10, 4)).last_due_at_ms is None
    assert manual.payload_json == {"auto_process": True}
    queue.succeed(manual)
    automatic = tick(session, settings, at_ms=ms(10, 5))
    assert automatic.last_due_at_ms == ms(10, 3)
    assert manual.payload_json == {"auto_process": False, "scheduled_due_at_ms": ms(10, 3)}


def test_empty_scope_never_enqueues_or_reads_real_provider(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.capture_provider = "douyin"
    enabled(session)
    monkeypatch.setattr(
        "douyin_knowledge.capture.registry.get_capture_provider",
        lambda _: pytest.fail("scheduler must not read real provider"),
    )
    result = tick(session, settings, at_ms=ms(10, 4))
    assert result.last_outcome == "skipped_empty_scope"
    assert result.last_job_id is None
    assert session.query(Job).count() == 0


def test_outcome_waits_for_targets_and_failure_is_not_rapidly_retried(
    session: Session, settings: Settings
) -> None:
    set_capture_scope(session, CaptureScope(named_collection_ids=["fixture-a"]))
    enabled(session)
    root_id = tick(session, settings, at_ms=ms(10, 4)).last_job_id
    assert root_id is not None
    queue = JobQueue(session)
    root = session.get(Job, root_id)
    assert root is not None
    queue.claim()
    from douyin_knowledge.capture.daily_sync import record_targets

    target = queue.enqueue(
        JobType.SYNC_NAMED_COLLECTION, payload={"auto_process": False},
        dedupe_key="sync_named_collection:douyin:fixture-a",
    ).job
    record_targets(session, ms(10, 3), [target.id])
    queue.succeed(root)
    assert refresh_outcome(session).last_outcome == "running"
    # A manual request may re-arm the terminal fan-out row while a target is
    # still pending. It must not erase the scheduled run's target outcome.
    queue.enqueue(
        JobType.SYNC_CAPTURE_SCOPE,
        payload={"auto_process": True}, dedupe_key="sync_capture_scope:douyin",
    )
    queue.fail(target, CaptureUnavailable("offline"), force_terminal=True)
    assert refresh_outcome(session).last_outcome == "failed"
    queue.enqueue(
        JobType.SYNC_NAMED_COLLECTION, payload={"auto_process": True},
        dedupe_key="sync_named_collection:douyin:fixture-a",
    )
    assert refresh_outcome(session).last_outcome == "failed"
    assert tick(session, settings, at_ms=ms(10, 6)).last_outcome == "failed"
    assert session.query(Job).count() == 2


def test_worker_fanout_is_metadata_only_and_scope_changes_are_honored(
    engine: object, settings: Settings
) -> None:
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=["demo-food"]))
        enabled(session)
        root_id = tick(session, settings, at_ms=ms(10, 4)).last_job_id
    worker = Worker(register_default_handlers(), settings=settings)
    assert worker.run_once()  # fan-out only
    with session_scope() as session:
        state = read_state(session)
        assert state.last_job_id == root_id
        assert state.last_outcome == "running"
        assert len(state.last_target_job_ids) == 1
        target = session.get(Job, state.last_target_job_ids[0])
        assert target is not None and target.payload_json["auto_process"] is False
        set_capture_scope(session, CaptureScope())
    assert worker.run_once()  # selected target is now skipped before provider access
    with session_scope() as session:
        assert refresh_outcome(session).last_outcome == "completed_with_skips"
        assert session.scalars(select(Job).where(Job.job_type == JobType.PROCESS_SOURCE)).all() == []


def test_api_save_and_reload(engine: object, settings: Settings) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        first = client.get("/api/admin/daily-sync")
        assert first.status_code == 200 and first.json()["enabled"] is False
        saved = client.put(
            "/api/admin/daily-sync",
            json={"enabled": True, "local_time": "03:00", "timezone": "Asia/Shanghai"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["timezone"] == "Asia/Shanghai"
        assert client.get("/api/admin/daily-sync").json()["enabled"] is True
        assert client.put(
            "/api/admin/daily-sync",
            json={"enabled": True, "local_time": "03:00", "timezone": "Not/AZone"},
        ).status_code == 422


def test_dst_boundary_uses_calendar_day_and_one_fall_occurrence() -> None:
    state = DailySyncUpdate(enabled=True, local_time="01:30", timezone="America/New_York")  # type: ignore[arg-type]
    from douyin_knowledge.capture.daily_sync import DailySyncState

    schedule = DailySyncState.model_validate(state.model_dump())
    due, following = due_and_next(schedule, int(datetime(2026, 11, 1, 7, tzinfo=timezone.utc).timestamp() * 1000))
    assert due == int(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert following == int(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc).timestamp() * 1000)

    spring = DailySyncState.model_validate(
        DailySyncUpdate(enabled=True, local_time="02:30", timezone="America/New_York").model_dump()  # type: ignore[arg-type]
    )
    gap_due, gap_next = due_and_next(
        spring, int(datetime(2026, 3, 8, 8, tzinfo=timezone.utc).timestamp() * 1000)
    )
    assert gap_due == int(datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert gap_next == int(datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc).timestamp() * 1000)


def test_stage3e_database_without_schedule_stays_disabled(
    session: Session, settings: Settings
) -> None:
    session.add(Collection(platform="douyin", external_collection_id="existing", name="Existing"))
    session.flush()
    assert read_state(session).enabled is False
    assert tick(session, settings, at_ms=ms(12, 4)).last_job_id is None
    assert session.query(Collection).count() == 1
    enabled(session)
    assert tick(session, settings, at_ms=ms(12, 4)).last_job_id is not None


def test_two_scheduler_instances_submit_one_due_slot(engine: object, settings: Settings) -> None:
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=["one"]))
        enabled(session)
    barrier = Barrier(2)

    def attempt() -> str | None:
        barrier.wait()
        with session_scope() as session:
            return tick(session, settings, at_ms=ms(10, 4)).last_job_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results[0] == results[1]
    with session_scope() as session:
        assert session.query(Job).count() == 1


def test_restart_catchup_uses_persisted_checkpoint(engine: object, settings: Settings) -> None:
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=["one"]))
        enabled(session)
    # Simulate a newly started worker on a later calendar day.
    with session_scope() as session:
        first = tick(session, settings, at_ms=ms(12, 5))
        assert first.last_due_at_ms == ms(12, 3)
        first_id = first.last_job_id
    with session_scope() as session:
        repeated = tick(session, settings, at_ms=ms(12, 6))
        assert repeated.last_job_id == first_id
        assert session.query(Job).count() == 1


def test_scheduled_fixture_sync_completes_without_processing(
    engine: object, settings: Settings
) -> None:
    with session_scope() as session:
        enabled(session)
        state = tick(session, settings, at_ms=ms(10, 4))
        assert state.last_job_id is not None
    worker = Worker(register_default_handlers(), settings=settings)
    assert worker.drain(max_jobs=30) == 4  # root plus three named targets
    with session_scope() as session:
        assert refresh_outcome(session).last_outcome == "succeeded"
        assert session.query(Source).count() == 8
        assert session.query(SourceSnapshot).count() == 8
        assert session.scalars(select(Job).where(Job.job_type == JobType.PROCESS_SOURCE)).all() == []


def test_missing_pinned_identity_fails_without_sidecar_request(
    engine: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.capture_provider = "douyin"
    settings.douyin_sidecar_identity = None
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(default_favorites=True))
        enabled(session)
        tick(session, settings, at_ms=ms(10, 4))

    def forbidden_request(*_args: object, **_kwargs: object) -> httpx.Response:
        pytest.fail("missing pin must fail before a sidecar request")

    monkeypatch.setattr(httpx.Client, "get", forbidden_request)
    worker = Worker(register_default_handlers(), settings=settings)
    assert worker.run_once()  # root fan-out
    assert worker.run_once()  # target fails closed
    with session_scope() as session:
        assert refresh_outcome(session).last_outcome == "failed"
        targets = session.scalars(select(Job).where(Job.job_type == JobType.SYNC_DEFAULT_FAVORITES)).all()
        assert len(targets) == 1
        assert targets[0].last_error_json["code"] == "authentication_required"


def test_retryable_target_failure_keeps_due_slot_and_stops_after_budget(
    session: Session, settings: Settings
) -> None:
    set_capture_scope(session, CaptureScope(named_collection_ids=["one"]))
    enabled(session)
    root_id = tick(session, settings, at_ms=ms(10, 4)).last_job_id
    assert root_id is not None
    queue = JobQueue(session)
    root = queue.claim()
    assert root is not None and root.id == root_id
    target = queue.enqueue(
        JobType.SYNC_NAMED_COLLECTION, payload={"auto_process": False},
        dedupe_key="sync_named_collection:douyin:one", max_attempts=2,
    ).job
    from douyin_knowledge.capture.daily_sync import record_targets

    record_targets(session, ms(10, 3), [target.id])
    queue.succeed(root)
    refresh_outcome(session)
    assert queue.claim().id == target.id  # type: ignore[union-attr]
    assert queue.fail(target, CaptureUnavailable("network"), retry_delay_ms=60_000)
    assert refresh_outcome(session).last_outcome == "running"
    target.available_at_ms = now_ms()
    session.flush()
    assert queue.claim().id == target.id  # type: ignore[union-attr]
    assert not queue.fail(target, CaptureUnavailable("network"))
    assert refresh_outcome(session).last_outcome == "failed"
    assert tick(session, settings, at_ms=ms(10, 5)).last_due_at_ms == ms(10, 3)
    assert session.query(Job).count() == 2
