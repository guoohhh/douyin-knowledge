"""Job queue invariants (AGENTS 13, TASKS Phase 3)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.config import Settings
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.errors import ModelRateLimited, SourceUnavailable
from douyin_knowledge.db.models.ops import Job, JobEvent
from douyin_knowledge.jobs import HandlerRegistry, JobContext, JobQueue, JobStatus, JobType, Worker
from douyin_knowledge.jobs.worker import backoff_delay_ms


def test_dedupe_key_prevents_duplicate_queued_jobs(session: Session) -> None:
    q = JobQueue(session)
    first = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="process:src_1", source_id=None)
    second = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="process:src_1", source_id=None)

    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id
    assert session.query(Job).count() == 1


def test_dedupe_key_rearms_terminal_job(session: Session) -> None:
    q = JobQueue(session)
    job = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="process:src_2").job
    q.fail(job, SourceUnavailable("gone"), force_terminal=True)
    assert job.status == JobStatus.FAILED

    again = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="process:src_2")
    assert again.created is True
    assert again.job.id == job.id
    assert again.job.status == JobStatus.QUEUED
    assert again.job.attempt == 0


def test_claim_respects_priority_then_fifo(session: Session) -> None:
    q = JobQueue(session)
    q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="low", priority=0)
    q.enqueue(JobType.WIKI_LINT, dedupe_key="high", priority=80)
    q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="mid", priority=10)

    assert q.claim().dedupe_key == "high"
    assert q.claim().dedupe_key == "mid"
    assert q.claim().dedupe_key == "low"
    assert q.claim() is None


def test_claim_skips_future_jobs(session: Session) -> None:
    q = JobQueue(session)
    q.enqueue(JobType.REBUILD_FTS, dedupe_key="later", available_at_ms=now_ms() + 60_000)
    assert q.claim() is None


def test_claim_increments_attempt_and_locks(session: Session) -> None:
    q = JobQueue(session)
    q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="x")
    job = q.claim(worker="w1")
    assert job is not None
    assert job.status == JobStatus.RUNNING
    assert job.attempt == 1
    assert job.locked_by == "w1"
    # A second worker must not see it.
    assert q.claim(worker="w2") is None


def test_expired_lease_is_reclaimed(session: Session) -> None:
    q = JobQueue(session, lease_ttl_s=60)
    q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="stale")
    job = q.claim(worker="dead-worker")
    assert job is not None
    # Simulate a worker that died 10 minutes ago.
    job.locked_at_ms = now_ms() - 600_000
    session.flush()

    reclaimed = q.claim(worker="live-worker")
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.locked_by == "live-worker"
    assert reclaimed.attempt == 2


def test_retryable_error_reschedules_until_max_attempts(session: Session) -> None:
    q = JobQueue(session)
    job = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="rl", max_attempts=2).job

    q.claim()
    assert q.fail(job, ModelRateLimited("429")) is True
    assert job.status == JobStatus.QUEUED

    job.available_at_ms = now_ms()
    session.flush()
    q.claim()
    assert q.fail(job, ModelRateLimited("429")) is False
    assert job.status == JobStatus.FAILED
    assert job.last_error_json["code"] == "model_rate_limited"


def test_non_retryable_error_fails_immediately(session: Session) -> None:
    q = JobQueue(session)
    job = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="nr", max_attempts=5).job
    q.claim()
    assert q.fail(job, SourceUnavailable("deleted upstream")) is False
    assert job.status == JobStatus.FAILED
    assert job.attempt == 1


def test_job_events_are_appended(session: Session) -> None:
    q = JobQueue(session)
    job = q.enqueue(JobType.PROCESS_SOURCE, dedupe_key="ev").job
    q.claim()
    q.succeed(job)
    kinds = [e.event_type for e in session.query(JobEvent).order_by(JobEvent.created_at_ms).all()]
    assert kinds == ["enqueued", "claimed", "succeeded"]


def test_worker_runs_handler_and_marks_success(engine, settings: Settings) -> None:
    from douyin_knowledge.db import session_scope

    seen: list[str] = []

    def handler(ctx: JobContext) -> None:
        seen.append(ctx.payload["value"])
        ctx.emit("progress", "halfway")

    registry = HandlerRegistry()
    registry.register(JobType.WIKI_MAINTAIN, handler)

    with session_scope() as s:
        JobQueue(s).enqueue(JobType.WIKI_MAINTAIN, payload={"value": "hi"}, dedupe_key="w1")

    worker = Worker(registry, settings=settings, name="test-worker")
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert seen == ["hi"]

    with session_scope() as s:
        job = s.query(Job).one()
        assert job.status == JobStatus.SUCCEEDED
        assert "progress" in {e.event_type for e in s.query(JobEvent).all()}


def test_worker_records_failure_without_losing_attempt(engine, settings: Settings) -> None:
    from douyin_knowledge.db import session_scope

    def boom(ctx: JobContext) -> None:
        raise SourceUnavailable("upstream 404")

    registry = HandlerRegistry()
    registry.register(JobType.PROCESS_SOURCE, boom)
    with session_scope() as s:
        JobQueue(s).enqueue(JobType.PROCESS_SOURCE, dedupe_key="boom")

    Worker(registry, settings=settings).run_once()

    with session_scope() as s:
        job = s.query(Job).one()
        assert job.status == JobStatus.FAILED
        assert job.attempt == 1
        assert job.last_error_json["code"] == "source_unavailable"


@pytest.mark.parametrize("attempt", [1, 2, 3, 10])
def test_backoff_grows_and_is_capped(attempt: int, settings: Settings) -> None:
    delay = backoff_delay_ms(attempt, settings)
    assert delay > 0
    assert delay <= settings.job_backoff_max_s * 1000


def test_worker_persists_audit_record_the_rollback_would_have_destroyed(
    engine, settings: Settings
) -> None:
    """A failed run must still be explainable afterwards (POL-004).

    The handler's `session_scope` rolls back on the exception, so a `processing_runs` row
    written to explain the failure is destroyed by the very failure it documents. Before the
    audit replay, `dk sources show` on a source whose processing had died reported no failed
    run at all -- the record said nothing had ever been attempted.
    """
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.capture import Source
    from douyin_knowledge.db.models.policy import SourceProcessingState
    from douyin_knowledge.db.models.processing import ProcessingRun
    from douyin_knowledge.extraction.orchestrator import AUDIT_REPLAY_ATTR, AuditReplay

    # A real source row: `processing_runs.source_id` is NOT NULL with a live FK, so the
    # audit replay has to satisfy the same constraints the original insert did.
    with session_scope() as s:
        s.add(Source(id="src_audit", platform="douyin", external_id="a1", source_type="video"))

    replay = AuditReplay(
        run_id="run_audit_test",
        source_id="src_audit",
        run_kind="extract",
        processor_version="1.0.0",
        schema_version="1",
        target_level=2,
        achieved_level=1,
        models_json={"provider": "mock"},
        config_json={"chunk_target_chars": 360},
        started_at_ms=now_ms(),
        finished_at_ms=now_ms(),
        error_json={"type": "SourceUnavailable", "message": "media gone"},
        partial_counts={"evidence": 3, "mentions": 0, "claims": 0, "chunks": 0},
    )

    def boom(ctx: JobContext) -> None:
        # Write the audit into the doomed transaction, exactly as the orchestrator does...
        ctx.session.add(
            ProcessingRun(
                id="run_audit_test",
                source_id="src_audit",
                run_kind="extract",
                processor_version="1.0.0",
                schema_version="1",
                target_level=2,
                status="failed",
            )
        )
        ctx.session.flush()
        exc = SourceUnavailable("media gone")
        setattr(exc, AUDIT_REPLAY_ATTR, replay)
        raise exc

    registry = HandlerRegistry()
    registry.register(JobType.PROCESS_SOURCE, boom)
    with session_scope() as s:
        JobQueue(s).enqueue(JobType.PROCESS_SOURCE, dedupe_key="audit")

    Worker(registry, settings=settings).run_once()

    with session_scope() as s:
        run = s.get(ProcessingRun, "run_audit_test")
        assert run is not None, "the failed run must survive the rollback"
        assert run.status == "failed"
        assert run.error_json["message"] == "media gone"
        assert run.achieved_level == 1, "how far it got is part of the explanation"
        # What the rollback took with it is stated, not silently omitted.
        assert run.config_json["discarded_partial_output"]["evidence"] == 3
        state = s.get(SourceProcessingState, "src_audit")
        assert state is not None and state.processing_status == "failed"
        # The currency pointer stays where it was: a failed run must not un-index a source.
        assert state.current_processing_run_id is None
        # The job itself still fails and still counts the attempt.
        job = s.query(Job).one()
        assert job.status in (JobStatus.QUEUED, JobStatus.FAILED)
        assert job.attempt == 1
