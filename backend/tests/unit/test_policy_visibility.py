"""An excluded source disappears from retrieval, and comes back on reversal.

`test_policy_retroactive.py` covers the reconciler's bookkeeping. This file covers the
part the user actually experiences, which is the brief's requirement in its own words: an
excluded source "immediately disappears from normal retrieval and must not contribute to
Wiki/current answers", and reversal "restores eligibility without losing history".

Keeping these separate matters because they can fail independently: the reconciler could
mark state correctly while retrieval ignores the column -- which is exactly the state the
codebase was in, with `current_policy_action` written by nobody and read by nobody.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.ai.adapters.mock_adapter import MockEmbeddingModel, MockStructuredModel
from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedCreator,
    CapturedMedia,
    CapturedSource,
)
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator
from douyin_knowledge.policy.models import PolicyAction, ProcessingRule, RuleType
from douyin_knowledge.policy.reconciler import PolicyReconciler
from douyin_knowledge.policy.repository import PolicyRepository
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.vector_store import VectorStore
from douyin_knowledge.search import indexer

QUERY = "人均"
SUBTITLE = "在中环有一家好运茶餐厅，叉烧饭人均八十块钱，出餐特别快。"


@pytest.fixture
def indexed(session: Session, settings: Settings) -> Source:
    """One processed, indexed, retrievable source."""
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
                caption_raw="在中环",
                creator=CapturedCreator(external_creator_id="c1", display_name="阿明"),
                media=[CapturedMedia(kind="subtitle", text=SUBTITLE, language="zh")],
            )
        ],
        collection=coll,
    )
    source = session.scalars(select(Source)).one()
    ProcessingOrchestrator(settings, structured_model=MockStructuredModel()).process_source(
        session, source.id, target_level=2
    )
    indexer.reindex_all(session)
    session.flush()
    return source


def exclude(session: Session, source_id: str) -> str:
    repo = PolicyRepository(session)
    rule = repo.save_rule(
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
    PolicyReconciler(session, repo).reconcile_rule(rule.id)
    return rule.id


def test_the_source_is_retrievable_to_begin_with(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """Guards the rest of the file: a query that never matched would prove nothing."""
    result = HybridRetriever(session).retrieve(QUERY, limit=5)
    assert not result.is_empty(), f"diagnostics: {result.diagnostics}"


def test_exclusion_removes_it_from_retrieval_without_reindexing(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """Exclusion takes effect at query time, not at the next reindex.

    The brief says "immediately". If this only worked after `reindex_all`, a user who
    excluded something would keep seeing it cited until a background job caught up.
    """
    exclude(session, indexed.id)

    result = HybridRetriever(session).retrieve(QUERY, limit=5)

    assert result.is_empty(), "an excluded source must not be citable"


def test_reversal_restores_retrieval(
    session: Session, settings: Settings, indexed: Source
) -> None:
    repo = PolicyRepository(session)
    rule_id = exclude(session, indexed.id)
    assert HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()

    reconciler = PolicyReconciler(session, repo)
    affected = reconciler.affected_source_ids(rule_id)
    repo.delete_rule(rule_id)
    reconciler.reconcile_sources(affected)

    result = HybridRetriever(session).retrieve(QUERY, limit=5)
    assert not result.is_empty(), "reversal must restore eligibility, not require reprocessing"


def test_reversal_needed_no_reprocessing(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """The point of not deleting: the run, evidence and claims are still the same rows."""
    runs = {r.id for r in session.scalars(select(ProcessingRun)).all()}
    evidence = {e.id for e in session.scalars(select(EvidenceUnit)).all()}
    claims = {c.id for c in session.scalars(select(Claim)).all()}
    assert runs and evidence

    repo = PolicyRepository(session)
    rule_id = exclude(session, indexed.id)
    reconciler = PolicyReconciler(session, repo)
    affected = reconciler.affected_source_ids(rule_id)
    repo.delete_rule(rule_id)
    reconciler.reconcile_sources(affected)

    assert {r.id for r in session.scalars(select(ProcessingRun)).all()} == runs
    assert {e.id for e in session.scalars(select(EvidenceUnit)).all()} == evidence
    assert {c.id for c in session.scalars(select(Claim)).all()} == claims


def test_reindexing_while_excluded_does_not_reintroduce_it(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """The alternate-index resurfacing path, which retrieval's own filter would mask.

    An excluded source that still had index rows would be one dropped filter away from
    being cited again, so the indexer refuses to write it rather than relying on the
    reader to exclude it.
    """
    exclude(session, indexed.id)

    store = VectorStore(settings.vector_dir)
    embedder = MockEmbeddingModel()
    indexer.reindex_all(session, store=store, embedder=embedder, model_name="mock-embedding")
    session.flush()

    result = HybridRetriever(session, vector_store=store, embedder=embedder).retrieve(
        QUERY, limit=5
    )
    assert result.is_empty(), "the vector path must not resurface an excluded source either"


def test_reindexing_after_reversal_restores_the_index(
    session: Session, settings: Settings, indexed: Source
) -> None:
    repo = PolicyRepository(session)
    rule_id = exclude(session, indexed.id)
    indexer.reindex_all(session)

    reconciler = PolicyReconciler(session, repo)
    affected = reconciler.affected_source_ids(rule_id)
    repo.delete_rule(rule_id)
    reconciler.reconcile_sources(affected)
    indexer.reindex_all(session)
    session.flush()

    assert not HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()


def test_metadata_only_removes_from_retrieval(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """`metadata_only` stops before knowledge processing; content was never understood.

    PROCESSING_POLICY §12: "metadata_only sources should not appear in normal AI knowledge
    answers because their content was never actually understood." This is distinct from
    `exclude` semantically, but both are HIDDEN_ACTIONS for retrieval/wiki/answers.
    """
    repo = PolicyRepository(session)
    rule = repo.save_rule(
        ProcessingRule(
            id="",
            name="shallow",
            is_enabled=True,
            rule_type=RuleType.SOURCE,
            action=PolicyAction.METADATA_ONLY,
            priority=0,
            target_source_id=indexed.id,
            origin="user",
        )
    )
    PolicyReconciler(session, repo).reconcile_rule(rule.id)

    assert HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()


def test_metadata_only_preserves_history_for_reversal(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """metadata_only -> process does not require reprocessing if already processed.

    A source processed, then switched to metadata_only, then back to process should still
    have its original runs/evidence/claims intact. History is preserved, only visibility
    changes.
    """
    runs_before = {r.id for r in session.scalars(select(ProcessingRun)).all()}
    evidence_before = {e.id for e in session.scalars(select(EvidenceUnit)).all()}
    claims_before = {c.id for c in session.scalars(select(Claim)).all()}
    assert runs_before and evidence_before

    repo = PolicyRepository(session)
    rule = repo.save_rule(
        ProcessingRule(
            id="",
            name="shallow",
            is_enabled=True,
            rule_type=RuleType.SOURCE,
            action=PolicyAction.METADATA_ONLY,
            priority=0,
            target_source_id=indexed.id,
            origin="user",
        )
    )
    reconciler = PolicyReconciler(session, repo)
    reconciler.reconcile_rule(rule.id)

    # While metadata_only, not retrievable
    assert HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()

    # Reverse the rule
    affected = reconciler.affected_source_ids(rule.id)
    repo.delete_rule(rule.id)
    reconciler.reconcile_sources(affected)

    # History intact, retrieval restored
    assert {r.id for r in session.scalars(select(ProcessingRun)).all()} == runs_before
    assert {e.id for e in session.scalars(select(EvidenceUnit)).all()} == evidence_before
    assert {c.id for c in session.scalars(select(Claim)).all()} == claims_before
    assert not HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()


def test_metadata_only_source_still_discoverable_via_explicit_metadata_search(
    session: Session, settings: Settings, indexed: Source
) -> None:
    """metadata_only sources hidden from knowledge retrieval, visible in metadata search.

    PROCESSING_POLICY §12: "basic metadata search may still allow queries such as:
    Show me videos I skipped from creator X"
    """
    repo = PolicyRepository(session)
    rule = repo.save_rule(
        ProcessingRule(
            id="",
            name="shallow",
            is_enabled=True,
            rule_type=RuleType.SOURCE,
            action=PolicyAction.METADATA_ONLY,
            priority=0,
            target_source_id=indexed.id,
            origin="user",
        )
    )
    PolicyReconciler(session, repo).reconcile_rule(rule.id)

    # Not in knowledge retrieval
    assert HybridRetriever(session).retrieve(QUERY, limit=5).is_empty()

    # But the Source row still exists and is queryable by metadata
    from douyin_knowledge.db.models.policy import SourceProcessingState

    state = session.get(SourceProcessingState, indexed.id)
    assert state is not None
    assert state.current_policy_action == PolicyAction.METADATA_ONLY.value

    # The source itself is still in the database
    source = session.get(Source, indexed.id)
    assert source is not None
    assert source.title == "一家茶餐厅"
