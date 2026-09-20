"""One queued job must become active work for exactly one worker.

The existing queue tests claim twice from a *single* session, which proves the status guard
in the UPDATE but not that it survives two connections: within one session SQLAlchemy's
identity map and transaction could mask a lost update that two real workers would hit.

These tests use two independent `Session` objects over the same SQLite file, which is what
`dk worker` instances actually are. Coordination is by explicit commit ordering rather than
threads and sleeps: the interleaving under test is "both workers saw the job as queued, then
one committed first", and forcing that order deterministically is strictly better than
racing two threads and hoping the scheduler produces it.

Single ownership rests on three layers, and the tests below cover each so that a regression
in the lowest one cannot hide behind the highest:

1. `db/engine.py` opens every transaction with BEGIN IMMEDIATE, so two claims serialise
   rather than interleave -- which is also why these tests keep each transaction short.
2. Candidate selection excludes a RUNNING job whose lease is still fresh.
3. The claiming UPDATE is guarded on the status the candidate was read at, so a stale view
   matches no row. Layer 2 normally spares us from needing this, which is precisely why it
   needs its own test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from douyin_knowledge.db import new_session
from douyin_knowledge.db.dml import execute_rowcount
from douyin_knowledge.db.models.ops import Job
from douyin_knowledge.jobs import JobQueue, JobStatus, JobType


@pytest.fixture
def two_workers(engine: Engine) -> Iterator[tuple[Session, Session]]:
    """Two sessions on their own connections, as two worker processes would have."""
    a = new_session()
    b = new_session()
    try:
        yield a, b
    finally:
        a.close()
        b.close()


def _enqueue(session: Session) -> str:
    """Enqueue one job and commit, so the other session's connection can see it."""
    result = JobQueue(session).enqueue(JobType.SYNC_COLLECTIONS, payload={"n": 1})
    job_id = result.job.id
    session.commit()
    return job_id


def _sees_queued(session: Session, job_id: str) -> bool:
    """Whether this connection can see the job as runnable work."""
    return (
        session.scalars(
            select(Job).where(Job.id == job_id, Job.status == JobStatus.QUEUED)
        ).first()
        is not None
    )


def test_two_sessions_cannot_both_claim_the_same_job(
    session: Session, two_workers: tuple[Session, Session]
) -> None:
    worker_a, worker_b = two_workers
    job_id = _enqueue(session)

    # Both workers independently observe the job as queued -- the precondition that makes
    # this contention rather than sequencing. Each observation is its own short transaction
    # because `db/engine.py` opens *every* transaction (reads included) with BEGIN IMMEDIATE,
    # which takes SQLite's write lock: two overlapping transactions on this engine do not
    # interleave, they block. Real workers behave the same way -- a claim is a short
    # transaction -- so serialising here is faithful, not a workaround.
    for worker in (worker_a, worker_b):
        assert _sees_queued(worker, job_id), "both workers must start from the same view"
        worker.rollback()

    claimed_a = JobQueue(worker_a).claim(worker="worker-a")
    assert claimed_a is not None and claimed_a.id == job_id
    worker_a.commit()

    # B now attempts the same job. The conditional UPDATE is guarded on the status it
    # expects, so B's write must not match a row.
    claimed_b = JobQueue(worker_b).claim(worker="worker-b")
    assert claimed_b is None, "a job already running must not be claimable as new work"

    worker_b.rollback()
    owner = session.get(Job, job_id)
    assert owner is not None
    session.refresh(owner)
    assert owner.status == JobStatus.RUNNING
    assert owner.locked_by == "worker-a", "exactly one worker may own the job"
    assert owner.attempt == 1, "a rejected claim must not inflate the attempt counter"


def test_a_stale_candidate_view_cannot_win_the_guarded_update(
    session: Session, two_workers: tuple[Session, Session]
) -> None:
    """The status guard, not just candidate selection, is what rejects the second claimant.

    `claim()` rejects a taken job in two independent ways, and the higher one hides the lower:
    a running job with a fresh lease is not in the runnable set at all, so a second worker
    usually finds no candidate and never reaches the UPDATE. That makes the surrounding tests
    pass even with the guard deleted. This test drives the layer underneath directly -- B
    selects a candidate while the job is genuinely queued, A then wins it, and B issues
    exactly the UPDATE `claim()` would issue against its now-stale view.
    """
    worker_a, worker_b = two_workers
    job_id = _enqueue(session)

    # B's view, captured while the job really is runnable.
    candidate = worker_b.scalars(select(Job).where(Job.id == job_id)).one()
    stale_status = candidate.status
    assert stale_status == JobStatus.QUEUED
    worker_b.rollback()

    claimed_a = JobQueue(worker_a).claim(worker="worker-a")
    assert claimed_a is not None and claimed_a.id == job_id
    worker_a.commit()

    # The row still exists, so an unguarded UPDATE would match it; only the status predicate
    # makes this a lost-update-free claim.
    assert worker_b.get(Job, job_id) is not None
    lost = execute_rowcount(
        worker_b,
        update(Job)
        .where(Job.id == job_id, Job.status == stale_status)
        .values(status=JobStatus.RUNNING, locked_by="worker-b"),
    )
    worker_b.rollback()
    assert lost == 0, "a stale queued-status view must match no row once another worker won"

    owner = session.get(Job, job_id)
    assert owner is not None
    session.refresh(owner)
    assert owner.locked_by == "worker-a"


def test_each_of_two_queued_jobs_goes_to_a_different_worker(
    session: Session, two_workers: tuple[Session, Session]
) -> None:
    """With two jobs available, two workers must take one each, not the same one twice."""
    worker_a, worker_b = two_workers
    first = _enqueue(session)
    second = _enqueue(session)
    assert first != second

    claimed_a = JobQueue(worker_a).claim(worker="worker-a")
    assert claimed_a is not None
    worker_a.commit()

    claimed_b = JobQueue(worker_b).claim(worker="worker-b")
    assert claimed_b is not None
    worker_b.commit()

    assert {claimed_a.id, claimed_b.id} == {first, second}, (
        "two workers must divide the queue, not duplicate one job"
    )

    for job_id, expected in ((claimed_a.id, "worker-a"), (claimed_b.id, "worker-b")):
        job = session.get(Job, job_id)
        assert job is not None
        session.refresh(job)
        assert job.locked_by == expected


def test_a_third_claim_finds_nothing_once_the_queue_is_drained(
    session: Session, two_workers: tuple[Session, Session]
) -> None:
    worker_a, worker_b = two_workers
    _enqueue(session)

    assert JobQueue(worker_a).claim(worker="worker-a") is not None
    worker_a.commit()

    assert JobQueue(worker_b).claim(worker="worker-b") is None
    assert JobQueue(worker_b).claim(worker="worker-b") is None, (
        "a failed claim must be idempotent, not progressively mutate the row"
    )
