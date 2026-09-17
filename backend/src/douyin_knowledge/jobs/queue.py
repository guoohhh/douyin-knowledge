"""SQLite-backed durable job queue.

Design notes:

* Claiming is a single ``UPDATE ... WHERE id = ? AND status = 'queued'`` guarded by
  ``BEGIN IMMEDIATE`` (see db/engine.py), which is how two workers on one SQLite file
  stay correct without row locks.
* ``dedupe_key`` is UNIQUE, so enqueueing the same logical work twice is a no-op
  rather than a duplicate run (JOB-002 idempotency).
* There is no ``lease_expires_at`` column: expiry is derived from
  ``locked_at_ms + lease_ttl_s`` so the timeout is a setting, not a migration.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.errors import DKError
from douyin_knowledge.db.models.ops import Job, JobEvent
from douyin_knowledge.jobs.types import JobStatus, JobType, Priority
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(slots=True)
class EnqueueResult:
    job: Job
    created: bool


class JobQueue:
    """Repository-style access to the ``jobs`` table. One instance per session."""

    def __init__(self, session: Session, *, lease_ttl_s: int = 600) -> None:
        self.session = session
        self.lease_ttl_s = lease_ttl_s

    # ---- producing -----------------------------------------------------
    def enqueue(
        self,
        job_type: JobType | str,
        *,
        payload: dict[str, Any] | None = None,
        source_id: str | None = None,
        dedupe_key: str | None = None,
        priority: int = Priority.NORMAL,
        available_at_ms: int | None = None,
        max_attempts: int = 3,
    ) -> EnqueueResult:
        """Insert a job, or return the existing one when ``dedupe_key`` collides.

        A dedupe collision against a *terminal* job re-arms that row instead of
        silently doing nothing, otherwise a permanent key like
        ``process_source:src_x`` could never be retried after a failure.
        """
        job_type = str(job_type)
        if dedupe_key:
            existing = self.session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
            if existing is not None:
                if existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    return EnqueueResult(existing, created=False)
                existing.status = JobStatus.QUEUED
                existing.attempt = 0
                existing.priority = max(existing.priority, priority)
                existing.payload_json = payload
                existing.available_at_ms = available_at_ms or now_ms()
                existing.locked_at_ms = None
                existing.locked_by = None
                existing.started_at_ms = None
                existing.finished_at_ms = None
                existing.last_error_json = None
                self.session.flush()
                self.log(existing, "requeued", "re-armed after terminal state")
                return EnqueueResult(existing, created=True)

        job = Job(
            job_type=job_type,
            status=JobStatus.QUEUED,
            priority=priority,
            source_id=source_id,
            payload_json=payload,
            dedupe_key=dedupe_key,
            max_attempts=max_attempts,
            available_at_ms=available_at_ms or now_ms(),
        )
        self.session.add(job)
        try:
            self.session.flush()
        except IntegrityError:
            # Lost a race on dedupe_key; the other writer's job is authoritative.
            self.session.rollback()
            existing = self.session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
            if existing is None:  # pragma: no cover - only on unrelated integrity errors
                raise
            return EnqueueResult(existing, created=False)
        self.log(job, "enqueued", f"{job_type} priority={priority}")
        return EnqueueResult(job, created=True)

    # ---- consuming -----------------------------------------------------
    def claim(self, *, worker: str | None = None, job_types: list[str] | None = None) -> Job | None:
        """Atomically take the highest-priority runnable job, or None."""
        worker = worker or worker_identity()
        ts = now_ms()
        lease_cutoff = ts - self.lease_ttl_s * 1000

        runnable = or_(
            and_(Job.status == JobStatus.QUEUED, Job.available_at_ms <= ts),
            # Reclaim a job whose worker died mid-flight.
            and_(
                Job.status == JobStatus.RUNNING,
                Job.locked_at_ms.is_not(None),
                Job.locked_at_ms < lease_cutoff,
            ),
        )
        stmt = select(Job).where(runnable)
        if job_types:
            stmt = stmt.where(Job.job_type.in_(job_types))
        stmt = stmt.order_by(Job.priority.desc(), Job.available_at_ms.asc()).limit(8)

        for candidate in self.session.scalars(stmt).all():
            expected_status = candidate.status
            result = self.session.execute(
                update(Job)
                .where(Job.id == candidate.id, Job.status == expected_status)
                .values(
                    status=JobStatus.RUNNING,
                    locked_at_ms=ts,
                    locked_by=worker,
                    started_at_ms=candidate.started_at_ms or ts,
                    attempt=Job.attempt + 1,
                )
            )
            if result.rowcount == 1:
                self.session.flush()
                self.session.refresh(candidate)
                reclaimed = expected_status == JobStatus.RUNNING
                self.log(
                    candidate,
                    "reclaimed" if reclaimed else "claimed",
                    f"worker={worker} attempt={candidate.attempt}",
                )
                return candidate
        return None

    def heartbeat(self, job: Job) -> None:
        """Extend the lease of a long-running job."""
        job.locked_at_ms = now_ms()
        self.session.flush()

    def succeed(self, job: Job, *, message: str | None = None, data: dict[str, Any] | None = None) -> None:
        job.status = JobStatus.SUCCEEDED
        job.finished_at_ms = now_ms()
        job.locked_at_ms = None
        job.locked_by = None
        job.last_error_json = None
        self.session.flush()
        self.log(job, "succeeded", message, data)

    def fail(
        self,
        job: Job,
        error: BaseException,
        *,
        retry_delay_ms: int | None = None,
        force_terminal: bool = False,
    ) -> bool:
        """Record a failure. Returns True if the job was rescheduled for retry."""
        payload = (
            error.to_dict()
            if isinstance(error, DKError)
            else {"code": type(error).__name__, "message": str(error), "retryable": False}
        )
        retryable = bool(payload.get("retryable")) and not force_terminal
        job.last_error_json = payload
        job.locked_at_ms = None
        job.locked_by = None

        if retryable and job.attempt < job.max_attempts:
            job.status = JobStatus.QUEUED
            job.available_at_ms = now_ms() + (retry_delay_ms or 5000)
            self.session.flush()
            self.log(job, "retry_scheduled", payload.get("message"), payload)
            return True

        job.status = JobStatus.FAILED
        job.finished_at_ms = now_ms()
        self.session.flush()
        self.log(job, "failed", payload.get("message"), payload)
        return False

    def cancel(self, job_id: str, *, reason: str = "cancelled by user") -> bool:
        job = self.session.get(Job, job_id)
        if job is None or job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return False
        job.status = JobStatus.CANCELLED
        job.finished_at_ms = now_ms()
        job.locked_at_ms = None
        job.locked_by = None
        self.session.flush()
        self.log(job, "cancelled", reason)
        return True

    # ---- diagnostics ---------------------------------------------------
    def log(
        self,
        job: Job,
        event_type: str,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            JobEvent(job_id=job.id, event_type=event_type, message=message, data_json=data)
        )
        self.session.flush()

    def counts_by_status(self) -> dict[str, int]:
        from sqlalchemy import func

        rows = self.session.execute(
            select(Job.status, func.count()).group_by(Job.status)
        ).all()
        return {str(status): count for status, count in rows}

    def pending_for_source(self, source_id: str) -> list[Job]:
        return list(
            self.session.scalars(
                select(Job).where(
                    Job.source_id == source_id,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            ).all()
        )
