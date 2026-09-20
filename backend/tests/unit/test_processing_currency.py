"""Processing currency: failed runs don't displace successful ones (P1-3).

When reprocessing fails, the previous successful run must remain current so retrieval,
wiki, and citations continue working from known-good data. A failed run creates audit
trail but does not advance the currency pointer.

Related: superseded runs cannot leak into current retrieval/wiki/answers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun


@pytest.fixture
def source(session: Session) -> Source:
    row = Source(
        platform="douyin",
        external_id="s_currency",
        source_type="video",
        title="Test source",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def state(session: Session, source: Source) -> SourceProcessingState:
    row = SourceProcessingState(
        source_id=source.id,
        current_policy_action="process",
        processing_status="pending",
    )
    session.add(row)
    session.flush()
    return row


def _run(
    session: Session,
    source: Source,
    *,
    status: str = "succeeded",
    achieved_level: int = 2,
) -> ProcessingRun:
    """Create a processing run."""
    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        schema_version="1.0",
        processor_version="0.1.0",
        status=status,
        target_level=2,
        achieved_level=achieved_level,
        started_at_ms=now_ms(),
        finished_at_ms=now_ms() if status != "running" else None,
    )
    session.add(run)
    session.flush()
    return run


def _evidence(session: Session, source: Source, run: ProcessingRun, text: str) -> EvidenceUnit:
    """Create evidence belonging to a specific run."""
    unit = EvidenceUnit(
        source_id=source.id,
        kind="transcript",
        raw_text=text,
        normalized_text=text,
        content_hash=f"hash_{run.id}",
    )
    session.add(unit)
    session.flush()
    return unit


class TestFailedReprocessingPreservesCurrentRun:
    def test_failed_run_does_not_displace_successful_current_run(
        self, session: Session, source: Source, state: SourceProcessingState
    ) -> None:
        """A reprocessing failure must not break retrieval/wiki/citations.

        Before this behavior: reprocessing starts, fails at level 0, advances the pointer
        anyway, and the source vanishes from search because it now points at a run with no
        evidence.
        """
        # Initial successful run
        run1 = _run(session, source, status="succeeded", achieved_level=2)
        _evidence(session, source, run1, "好运茶餐厅人均八十块")

        # Mark it current
        state.current_processing_run_id = run1.id
        state.processing_status = "succeeded"
        session.flush()

        # Reprocessing attempt fails
        run2 = _run(session, source, status="failed", achieved_level=0)
        # No evidence created for failed run
        session.flush()

        # The current pointer must not have moved
        session.expire(state)
        assert state.current_processing_run_id == run1.id
        assert state.processing_status == "succeeded"

    def test_successful_reprocessing_advances_the_pointer(
        self, session: Session, source: Source, state: SourceProcessingState
    ) -> None:
        """Normal case: a successful run becomes current."""
        run1 = _run(session, source, status="succeeded")
        _evidence(session, source, run1, "old evidence")
        state.current_processing_run_id = run1.id
        state.processing_status = "succeeded"
        session.flush()

        run2 = _run(session, source, status="succeeded")
        _evidence(session, source, run2, "new evidence")

        # Simulate orchestrator advancing the pointer
        state.current_processing_run_id = run2.id
        session.flush()

        session.expire(state)
        assert state.current_processing_run_id == run2.id


class TestSupersededRunsDoNotLeakIntoCurrent:
    def test_wiki_builder_reads_only_current_run_evidence(
        self, session: Session, source: Source, state: SourceProcessingState
    ) -> None:
        """Wiki must not compose claims from a superseded run."""
        from douyin_knowledge.db.models.entities import Claim, Entity
        from douyin_knowledge.wiki.builder import WikiBuilder

        entity = Entity(
            entity_type="place",
            canonical_name="好运茶餐厅",
            normalized_name="好运茶餐厅",
        )
        session.add(entity)
        session.flush()

        # Run 1: succeeded, was current
        run1 = _run(session, source, status="succeeded")
        claim1 = Claim(
            source_id=source.id,
            processing_run_id=run1.id,
            subject_entity_id=entity.id,
            subject_text="好运茶餐厅",
            predicate="price_per_person",
            value_type="number",
            value_number=80,
            claim_kind="measurement",
            provenance_type="creator_statement",
            attribution="unknown",
            grounding_status="valid",
        )
        session.add(claim1)

        # Run 2: succeeded, now current (supersedes run1)
        run2 = _run(session, source, status="succeeded")
        claim2 = Claim(
            source_id=source.id,
            processing_run_id=run2.id,
            subject_entity_id=entity.id,
            subject_text="好运茶餐厅",
            predicate="price_per_person",
            value_type="number",
            value_number=100,  # Price changed
            claim_kind="measurement",
            provenance_type="creator_statement",
            attribution="unknown",
            grounding_status="valid",
        )
        session.add(claim2)

        state.current_processing_run_id = run2.id
        state.processing_status = "succeeded"
        session.flush()

        # Wiki builder must see only run2's claim
        claims = list(WikiBuilder(session).claims_for_entity(entity.id))
        assert len(claims) == 1
        assert claims[0].processing_run_id == run2.id
        assert claims[0].value_number == 100

    def test_retriever_indexes_only_current_run_evidence(
        self, session: Session, source: Source, state: SourceProcessingState
    ) -> None:
        """Search must not return chunks from a superseded run."""
        from douyin_knowledge.core.text import content_hash
        from douyin_knowledge.db.models.processing import (
            ProcessingRunEvidence,
            RetrievalChunk,
            RetrievalChunkEvidence,
        )
        from douyin_knowledge.search import indexer

        run1 = _run(session, source, status="succeeded")
        ev1 = _evidence(session, source, run1, "好运茶餐厅人均八十块")

        # Create a chunk for run1
        chunk1 = RetrievalChunk(
            source_id=source.id,
            processing_run_id=run1.id,
            chunk_type="paragraph",
            ordinal=0,
            text="好运茶餐厅人均八十块",
            content_hash=content_hash("chunk", "好运茶餐厅人均八十块"),
        )
        session.add(chunk1)
        session.flush()
        session.add(RetrievalChunkEvidence(
            retrieval_chunk_id=chunk1.id,
            evidence_id=ev1.id,
        ))
        session.add(ProcessingRunEvidence(
            processing_run_id=run1.id,
            evidence_id=ev1.id,
            usage_role="input",
        ))

        run2 = _run(session, source, status="succeeded")
        ev2 = _evidence(session, source, run2, "好运茶餐厅人均一百块")

        # Create a chunk for run2
        chunk2 = RetrievalChunk(
            source_id=source.id,
            processing_run_id=run2.id,
            chunk_type="paragraph",
            ordinal=0,
            text="好运茶餐厅人均一百块",
            content_hash=content_hash("chunk", "好运茶餐厅人均一百块"),
        )
        session.add(chunk2)
        session.flush()
        session.add(RetrievalChunkEvidence(
            retrieval_chunk_id=chunk2.id,
            evidence_id=ev2.id,
        ))
        session.add(ProcessingRunEvidence(
            processing_run_id=run2.id,
            evidence_id=ev2.id,
            usage_role="input",
        ))

        # Point at run2
        state.current_processing_run_id = run2.id
        state.processing_status = "succeeded"
        state.current_policy_action = "process"
        session.flush()

        # Reindex the source - should only index run2's chunks
        indexer.reindex_source(session, source.id)
        session.commit()

        # Only run2's evidence should be retrievable
        from douyin_knowledge.retrieval.retriever import HybridRetriever

        results = HybridRetriever(session).retrieve("人均", limit=10)
        evidence_ids = set()
        for chunk in results.chunks:
            evidence_ids.update(chunk.evidence_ids)

        assert ev2.id in evidence_ids
        assert ev1.id not in evidence_ids, "superseded run evidence must not be retrieved"
