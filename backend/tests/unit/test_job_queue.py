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
    q.enqueue(JobType.CLEANUP_CACHE, dedupe_key="later", available_at_ms=now_ms() + 60_000)
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
    registry.register(JobType.EXPORT_MARKDOWN, handler)

    with session_scope() as s:
        JobQueue(s).enqueue(JobType.EXPORT_MARKDOWN, payload={"value": "hi"}, dedupe_key="w1")

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
