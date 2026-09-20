"""Policy changing while jobs are already queued (P0-3).

This is the case with a real failure mode. A bulk sync can take a long time to drain, and
the decision must reflect the rules as they stand when each job *runs* rather than when it
was enqueued -- otherwise a user who excludes a creator mid-sync still pays for the
extraction of everything already in the queue.

The API surface for policy is covered in `tests/test_api.py`, where the app fixture and a
real drained corpus already live.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedCreator,
    CapturedMedia,
    CapturedSource,
)
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import ProcessingRun
from douyin_knowledge.jobs import handlers as handlers_module
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.registry import JobContext
from douyin_knowledge.jobs.types import JobType
from douyin_knowledge.policy.models import PolicyAction, ProcessingRule, RuleType
from douyin_knowledge.policy.repository import PolicyRepository

SUBTITLE = "在中环有一家好运茶餐厅，叉烧饭人均八十块钱。"


@pytest.fixture
def synced(session: Session) -> Source:
    service = CaptureSyncService(session)
    coll = service.upsert_collection(
        CapturedCollection(external_collection_id="col1", name="收藏夹")
    )
    service.sync_sources(
        [
            CapturedSource(
                platform="douyin",
                external_id="7100",
                title="一家茶餐厅",
                creator=CapturedCreator(external_creator_id="c1", display_name="阿明"),
                media=[CapturedMedia(kind="subtitle", text=SUBTITLE, language="zh")],
            )
        ],
        collection=coll,
    )
    return session.scalars(select(Source)).one()


def enqueue_processing(session: Session, settings: Settings, source: Source) -> JobContext:
    queue = JobQueue(session)
    job = queue.enqueue(
        JobType.PROCESS_SOURCE,
        source_id=source.id,
        payload={"source_id": source.id, "target_level": 2},
    ).job
    return JobContext(job=job, session=session, queue=queue, settings=settings)


def exclude_rule(session: Session, source_id: str) -> ProcessingRule:
    return PolicyRepository(session).save_rule(
        ProcessingRule(
            id="",
            name="exclude",
            is_enabled=True,
            rule_type=RuleType.SOURCE,
            action=PolicyAction.EXCLUDE,
            priority=0,
            target_source_id=source_id,
            origin="user",
        )
    )


def test_a_rule_added_after_enqueue_is_respected(
    session: Session, settings: Settings, synced: Source
) -> None:
    """Queued while eligible, excluded by the time it ran."""
    ctx = enqueue_processing(session, settings, synced)
    exclude_rule(session, synced.id)

    handlers_module.handle_process_source(ctx)

    assert session.scalars(select(ProcessingRun)).all() == [], "no extraction for an excluded source"
    state = session.get(SourceProcessingState, synced.id)
    assert state is not None
    assert state.current_policy_action == PolicyAction.EXCLUDE.value


def test_the_gate_records_its_verdict_on_the_state_row(
    session: Session, settings: Settings, synced: Source
) -> None:
    """The gate and the reconciler must not be able to disagree.

    Before this, the gate evaluated policy and discarded the verdict, so a source excluded
    at processing time still read `process` in the column retrieval filters on -- two
    mechanisms with two different answers about the same source.
    """
    ctx = enqueue_processing(session, settings, synced)

    handlers_module.handle_process_source(ctx)

    state = session.get(SourceProcessingState, synced.id)
    assert state is not None
    assert state.current_policy_action == PolicyAction.PROCESS.value
    assert state.current_policy_decision_id is not None


def test_an_excluded_source_records_why(
    session: Session, settings: Settings, synced: Source
) -> None:
    """"Why did this not get processed?" must be answerable from the record (POL-004)."""
    ctx = enqueue_processing(session, settings, synced)
    rule = exclude_rule(session, synced.id)

    handlers_module.handle_process_source(ctx)

    decision = PolicyRepository(session).get_latest_decision(synced.id)
    assert decision is not None
    assert decision.action == PolicyAction.EXCLUDE
    assert decision.rule_id == rule.id
