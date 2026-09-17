"""Worker loop.

Single-threaded by default: local-first, one SQLite file, and every expensive step is
already IO-bound on an external provider. ``concurrency`` spawns additional threads
when a user really does have thousands of items to backfill.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.core.errors import DKError
from douyin_knowledge.db.session import session_scope
from douyin_knowledge.jobs.queue import JobQueue, worker_identity
from douyin_knowledge.jobs.registry import HandlerRegistry, JobContext
from douyin_knowledge.observability import get_logger, log_context

logger = get_logger(__name__)


def backoff_delay_ms(attempt: int, settings: Settings) -> int:
    """Exponential backoff with jitter, capped. Jitter avoids retry stampedes when a
    provider rate-limits a whole batch at once."""
    base = settings.job_backoff_base_s * (2 ** max(0, attempt - 1))
    capped = min(base, settings.job_backoff_max_s)
    jittered = capped * (0.5 + random.random() * 0.5)
    return int(jittered * 1000)


class Worker:
    def __init__(
        self,
        registry: HandlerRegistry,
        *,
        settings: Settings | None = None,
        name: str | None = None,
        job_types: list[str] | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings or get_settings()
        self.name = name or worker_identity()
        self.job_types = job_types
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ---- single step ----------------------------------------------------
    def run_once(self) -> bool:
        """Claim and execute at most one job. Returns True if work was done.

        The claim, the handler and the outcome each get their own transaction: a
        handler that dies mid-way must not roll back the fact that it was claimed,
        or the attempt counter would never advance and a poison job would loop.
        """
        with session_scope() as session:
            queue = JobQueue(session, lease_ttl_s=self.settings.worker_lease_ttl_s)
            job = queue.claim(worker=self.name, job_types=self.job_types)
            if job is None:
                return False
            job_id, job_type, attempt = job.id, job.job_type, job.attempt
            source_id = job.source_id

        with log_context(job_id=job_id, job_type=job_type, source_id=source_id, attempt=attempt):
            started = time.monotonic()
            try:
                with session_scope() as session:
                    queue = JobQueue(session, lease_ttl_s=self.settings.worker_lease_ttl_s)
                    fresh = session.get(type(job), job_id)
                    assert fresh is not None
                    handler = self.registry.get(job_type)
                    handler(JobContext(fresh, session, queue, self.settings))
                    queue.succeed(fresh, message=f"{time.monotonic() - started:.2f}s")
                logger.info("job succeeded", extra={"duration_s": round(time.monotonic() - started, 3)})
            except Exception as exc:  # noqa: BLE001 - the worker is the failure boundary
                retried = self._record_failure(job_id, attempt, exc)
                logger.warning(
                    "job failed",
                    extra={
                        "error_code": exc.code if isinstance(exc, DKError) else type(exc).__name__,
                        "retried": retried,
                    },
                    exc_info=not isinstance(exc, DKError),
                )
        return True

    def _record_failure(self, job_id: str, attempt: int, exc: BaseException) -> bool:
        try:
            with session_scope() as session:
                from douyin_knowledge.db.models.ops import Job

                queue = JobQueue(session, lease_ttl_s=self.settings.worker_lease_ttl_s)
                fresh = session.get(Job, job_id)
                if fresh is None:  # pragma: no cover
                    return False
                return queue.fail(fresh, exc, retry_delay_ms=backoff_delay_ms(attempt, self.settings))
        except Exception:  # pragma: no cover - never let bookkeeping kill the worker
            logger.exception("failed to record job failure")
            return False

    # ---- loops ----------------------------------------------------------
    def run_forever(self, *, on_idle: Callable[[], None] | None = None) -> None:
        logger.info("worker started", extra={"worker": self.name, "types": self.registry.known_types()})
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception:  # pragma: no cover
                logger.exception("worker loop error")
                did_work = False
            if did_work:
                self._stop.wait(self.settings.worker_batch_sleep_s)
            else:
                if on_idle is not None:
                    on_idle()
                self._stop.wait(self.settings.worker_poll_interval_s)
        logger.info("worker stopped", extra={"worker": self.name})

    def drain(self, *, max_jobs: int = 1000) -> int:
        """Run until the queue is empty. Used by the CLI and integration tests."""
        done = 0
        while done < max_jobs and self.run_once():
            done += 1
        return done
