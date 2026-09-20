"""Policy must be reversible and retroactive (P0-3).

The pre-existing evaluator is a *gate*: it decides whether to spend model calls before
processing. That is necessary and not sufficient. A user who processes a source and then
excludes it expects it to disappear from answers, and `SourceProcessingState` has carried
`current_policy_action` and `current_policy_decision_id` since the first migration with
nothing reading or writing either one -- so exclusion after the fact had no effect at all.

The constraint that shapes every test here: "Do not achieve this by deleting historical
Claims/Evidence/Runs." Exclusion is a visibility change, not a delete. Reversal must
therefore restore eligibility without anything having been lost.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.ai.adapters.mock_adapter import MockStructuredModel
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
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator
from douyin_knowledge.policy.models import PolicyAction, ProcessingRule, RuleType
from douyin_knowledge.policy.reconciler import PolicyReconciler
from douyin_knowledge.policy.repository import PolicyRepository

SUBTITLE = "在中环有一家好运茶餐厅，叉烧饭人均八十块钱。"


@pytest.fixture
def processed(session: Session, settings: Settings) -> Source:
    """One fully processed source, reachable by retrieval."""
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
                caption_raw="在中环 #美食",
                creator=CapturedCreator(external_creator_id="c1", display_name="阿明"),
                media=[CapturedMedia(kind="subtitle", text=SUBTITLE, language="zh")],
            )
        ],
        collection=coll,
    )
    source = session.scalars(select(Source)).one()
    orchestrator = ProcessingOrchestrator(settings, structured_model=MockStructuredModel())
    orchestrator.process_source(session, source.id, target_level=2)
    session.flush()
    return source


def reconcile(session: Session) -> PolicyReconciler:
    return PolicyReconciler(session, PolicyRepository(session))


def state_of(session: Session, source_id: str) -> SourceProcessingState:
    state = session.get(SourceProcessingState, source_id)
    assert state is not None
    return state


def exclude_source_rule(source_id: str) -> ProcessingRule:
    return ProcessingRule(
        id="",
        name="exclude this one",
        is_enabled=True,
        rule_type=RuleType.SOURCE,
        action=PolicyAction.EXCLUDE,
        priority=0,
        target_source_id=source_id,
        origin="user",
    )


class TestRetroactiveExclusion:
    def test_processed_source_starts_eligible(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        state = state_of(session, processed.id)
        assert state.current_policy_action == PolicyAction.PROCESS.value
        assert state.current_processing_run_id is not None

    def test_excluding_after_processing_marks_state_excluded(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """The gate cannot do this: the model calls already happened."""
        repo = PolicyRepository(session)
        rule = repo.save_rule(exclude_source_rule(processed.id))

        summary = reconcile(session).reconcile_rule(rule.id)

        assert summary.reevaluated == 1
        assert summary.changed == 1
        state = state_of(session, processed.id)
        assert state.current_policy_action == PolicyAction.EXCLUDE.value
        assert state.current_policy_decision_id is not None, "the state must point at its reason"

    def test_exclusion_preserves_history(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """"Do not achieve this by deleting historical Claims/Evidence/Runs.\""""
        runs_before = session.scalars(select(ProcessingRun)).all()
        evidence_before = session.scalars(select(EvidenceUnit)).all()
        assert runs_before and evidence_before

        repo = PolicyRepository(session)
        rule = repo.save_rule(exclude_source_rule(processed.id))
        reconcile(session).reconcile_rule(rule.id)

        assert len(session.scalars(select(ProcessingRun)).all()) == len(runs_before)
        assert len(session.scalars(select(EvidenceUnit)).all()) == len(evidence_before)
        state = state_of(session, processed.id)
        assert state.current_processing_run_id is not None, "the run pointer survives exclusion"

    def test_removing_the_rule_restores_eligibility(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        repo = PolicyRepository(session)
        rule = repo.save_rule(exclude_source_rule(processed.id))
        reconcile(session).reconcile_rule(rule.id)
        assert state_of(session, processed.id).current_policy_action == PolicyAction.EXCLUDE.value

        affected = reconcile(session).affected_source_ids(rule.id)
        repo.delete_rule(rule.id)
        summary = reconcile(session).reconcile_sources(affected)

        assert summary.changed == 1
        state = state_of(session, processed.id)
        assert state.current_policy_action == PolicyAction.PROCESS.value
        assert state.current_processing_run_id is not None, "history was never discarded"

    def test_disabling_the_rule_restores_eligibility(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """Disable is the reversible form of delete and must behave the same way."""
        repo = PolicyRepository(session)
        rule = repo.save_rule(exclude_source_rule(processed.id))
        reconcile(session).reconcile_rule(rule.id)

        repo.set_rule_enabled(rule.id, False)
        reconcile(session).reconcile_rule(rule.id)

        assert state_of(session, processed.id).current_policy_action == PolicyAction.PROCESS.value


class TestPrecedenceUnderReconciliation:
    def test_per_source_always_process_beats_creator_exclude(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """The brief's case: a broad creator exclude plus a per-source pin."""
        repo = PolicyRepository(session)
        creator_rule = repo.save_rule(
            ProcessingRule(
                id="",
                name="exclude the creator",
                is_enabled=True,
                rule_type=RuleType.CREATOR,
                action=PolicyAction.EXCLUDE,
                priority=0,
                target_creator_id=processed.creator_id,
                origin="user",
            )
        )
        reconcile(session).reconcile_rule(creator_rule.id)
        assert state_of(session, processed.id).current_policy_action == PolicyAction.EXCLUDE.value

        pin = repo.save_rule(
            ProcessingRule(
                id="",
                name="but keep this one",
                is_enabled=True,
                rule_type=RuleType.SOURCE,
                action=PolicyAction.ALWAYS_PROCESS,
                priority=0,
                target_source_id=processed.id,
                origin="user",
            )
        )
        reconcile(session).reconcile_rule(pin.id)

        state = state_of(session, processed.id)
        assert state.current_policy_action == PolicyAction.ALWAYS_PROCESS.value

    def test_collection_rule_reaches_its_members(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        from douyin_knowledge.db.models.capture import Collection

        collection = session.scalars(select(Collection)).one()
        repo = PolicyRepository(session)
        rule = repo.save_rule(
            ProcessingRule(
                id="",
                name="exclude the folder",
                is_enabled=True,
                rule_type=RuleType.COLLECTION,
                action=PolicyAction.EXCLUDE,
                priority=0,
                target_collection_id=collection.id,
                origin="user",
            )
        )

        summary = reconcile(session).reconcile_rule(rule.id)

        assert summary.reevaluated >= 1
        assert state_of(session, processed.id).current_policy_action == PolicyAction.EXCLUDE.value

    def test_metadata_rule_reconciles_over_all_sources(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """A matcher has no target id, so the affected set is every source.

        This is the expensive case and the reason `affected_source_ids` exists as its own
        method: a per-source rule must not trigger a full-corpus walk.
        """
        repo = PolicyRepository(session)
        rule = repo.save_rule(
            ProcessingRule(
                id="",
                name="no 美食 clips",
                is_enabled=True,
                rule_type=RuleType.METADATA,
                action=PolicyAction.EXCLUDE,
                priority=0,
                matcher_json={"hashtags": ["美食"]},
                origin="user",
            )
        )

        summary = reconcile(session).reconcile_rule(rule.id)

        assert summary.changed == 1
        assert state_of(session, processed.id).current_policy_action == PolicyAction.EXCLUDE.value


class TestReconciliationIsNarrow:
    def test_per_source_rule_does_not_walk_the_corpus(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        repo = PolicyRepository(session)
        rule = repo.save_rule(exclude_source_rule(processed.id))

        affected = reconcile(session).affected_source_ids(rule.id)

        assert affected == [processed.id]

    def test_reconciling_an_unchanged_source_reports_no_change(
        self, session: Session, settings: Settings, processed: Source
    ) -> None:
        """Idempotence: the second pass must not churn decisions.

        Every reconcile records a decision when the action *changes*; recording one every
        time would fill the audit log with "still eligible" noise on every rule edit.
        """
        first = reconcile(session).reconcile_sources([processed.id])
        second = reconcile(session).reconcile_sources([processed.id])

        assert second.reevaluated == 1
        assert second.changed == 0
        assert first.reevaluated == 1
