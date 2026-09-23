"""The provenance invariant holds on the plain hybrid path too, not just the structured one.

    a Claim used as presented knowledge must have at least one valid same-source EvidenceUnit

Structured retrieval was fixed to enforce that. Plain hybrid retrieval kept a parallel,
weaker definition: `_claims_for_chunks` hand-checked run currency and assertability, which
is two of the six eligibility rules. The gap was reachable. Given the corrupted shape

    Claim(source=A) -> ClaimEvidence -> EvidenceUnit(source=B)

retrieving B's chunk pulled A's claim in on B's evidence -- the same fabricated provenance,
through a different door. `GroundingValidator` refuses to persist that shape, so these tests
construct it directly: the point of a fail-closed check is what it does with data that should
not exist.

`CitationBuilder` had the mirror of it. `_claim_evidence_map` correctly dropped the crossed
row, and then `build()` minted a `precision="claim"` citation for the claim anyway -- a
citation asserting claim-level precision with no evidence behind it at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.citation_builder import CitationBuilder
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.retrieval.retriever import HybridRetriever, RetrievalResult
from tests.test_structured_retrieval import Corpus, _index


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _claim_of(session: Session, entity: Entity, predicate: str) -> Claim:
    claim = session.scalar(
        select(Claim).where(
            Claim.subject_entity_id == entity.id, Claim.predicate == predicate
        )
    )
    assert claim is not None
    return claim


def _hybrid(session: Session, query: str) -> RetrievalResult:
    _index(session)
    return HybridRetriever(session).retrieve(query, limit=10, include_claims=True)


class TestHybridRetrievalRejectsCrossedProvenance:
    """Gap 1a. The crossed shape must not be selectable by ordinary hybrid retrieval."""

    def test_a_claim_whose_only_evidence_is_another_source_is_not_returned(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant(
            "跨来源证据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="跨来源证据店 旺角 日料 人均80",
        )
        other_source, other_run = corpus.source("第二来源")
        claim = _claim_of(session, place, "cuisine")

        # Repoint every evidence unit behind this claim at a different source, which is the
        # corrupted persisted shape. The claim keeps source A; its evidence now says B.
        evidence_ids = list(
            session.scalars(
                select(ClaimEvidence.evidence_id).where(ClaimEvidence.claim_id == claim.id)
            )
        )
        assert evidence_ids
        for evidence in session.scalars(
            select(EvidenceUnit).where(EvidenceUnit.id.in_(evidence_ids))
        ):
            evidence.source_id = other_source.id
            evidence.processing_run_id = other_run.id
        session.flush()

        result = _hybrid(session, "跨来源证据店 日料")

        assert claim.id not in [c.id for c in result.claims]

    def test_a_claim_with_no_evidence_at_all_is_not_returned(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Held before this change too, but incidentally: deleting the `ClaimEvidence` rows
        also removes the evidence overlap the relevance join needs, so the claim was never
        selected rather than being selected and rejected. Kept because it now holds for the
        stated reason, and would survive a future change to how candidates are gathered.
        """
        place = corpus.restaurant(
            "无证据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="无证据店 旺角 日料 人均80",
        )
        claim = _claim_of(session, place, "cuisine")
        session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id))
        session.flush()

        result = _hybrid(session, "无证据店 日料")

        assert claim.id not in [c.id for c in result.claims]

    def test_hidden_policy_action_also_excludes_the_claim(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Rule 3, which also held before, by a different route.

        The old code received a `current` map built by `current_eligible_runs`, which applies
        policy and currency, so a hidden source's claims were already excluded. Rule 3 is now
        enforced by the same predicate as the rest rather than by what the caller passed in,
        and this pins that it did not regress in the swap.
        """
        from douyin_knowledge.db.models.policy import SourceProcessingState

        place = corpus.restaurant(
            "被隐藏的店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="被隐藏的店 旺角 日料 人均80",
        )
        claim = _claim_of(session, place, "cuisine")
        state = session.get(SourceProcessingState, claim.source_id)
        assert state is not None
        state.current_policy_action = "exclude"
        session.flush()

        result = _hybrid(session, "被隐藏的店 日料")

        assert claim.id not in [c.id for c in result.claims]


class TestHealthyHybridClaimRetrievalIsUnchanged:
    """Gap 1c. The invariant must not be enforced by returning nothing."""

    def test_a_healthy_claim_is_still_returned_with_its_chunk(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant(
            "健康的旺角店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="健康的旺角店 旺角 日料 人均80",
        )

        result = _hybrid(session, "健康的旺角店 日料")

        assert result.chunks
        assert result.claims
        for claim in result.claims:
            same_source = session.scalar(
                select(EvidenceUnit.id)
                .join(ClaimEvidence, ClaimEvidence.evidence_id == EvidenceUnit.id)
                .where(
                    ClaimEvidence.claim_id == claim.id,
                    EvidenceUnit.source_id == claim.source_id,
                )
            )
            assert same_source is not None

    def test_the_healthy_claim_still_earns_a_citation(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant(
            "健康的旺角店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="健康的旺角店 旺角 日料 人均80",
        )

        result = _hybrid(session, "健康的旺角店 日料")
        citations = CitationBuilder(session).build(result)

        assert result.claims
        assert all(claim.id in citations.by_claim for claim in result.claims)


class TestCitationBuilderFailsClosed:
    """Gap 1b. The last gate: never mint a claim citation with no valid evidence."""

    def test_no_claim_citation_is_minted_without_same_source_evidence(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Handed the claim directly, bypassing retrieval's eligibility filter."""
        place = corpus.restaurant(
            "无证据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="无证据店 旺角 日料 人均80",
        )
        claim = _claim_of(session, place, "cuisine")
        session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id))
        session.flush()

        citations = CitationBuilder(session).build(
            RetrievalResult(query="无证据店", claims=[claim])
        )

        assert citations.by_claim == {}
        assert citations.citations == []

    def test_no_claim_citation_is_minted_for_crossed_evidence(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant(
            "跨来源证据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="跨来源证据店 旺角 日料 人均80",
        )
        other_source, other_run = corpus.source("第二来源")
        claim = _claim_of(session, place, "cuisine")
        for evidence in session.scalars(
            select(EvidenceUnit)
            .join(ClaimEvidence, ClaimEvidence.evidence_id == EvidenceUnit.id)
            .where(ClaimEvidence.claim_id == claim.id)
        ):
            evidence.source_id = other_source.id
            evidence.processing_run_id = other_run.id
        session.flush()

        citations = CitationBuilder(session).build(
            RetrievalResult(query="跨来源证据店", claims=[claim])
        )

        assert citations.by_claim == {}
        assert all(c.claim_id != claim.id for c in citations.citations)

    def test_a_claim_whose_evidence_exists_but_was_not_retrieved_still_cites(
        self, session: Session, corpus: Corpus
    ) -> None:
        """The distinction the fix turns on.

        "No same-source evidence exists" is corruption and must fail closed. "Its evidence
        exists but no chunk for it was retrieved" is the ordinary source-level case, and
        suppressing that would silently drop legitimate citations.
        """
        place = corpus.restaurant(
            "健康的旺角店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="健康的旺角店 旺角 日料 人均80",
        )
        claim = _claim_of(session, place, "cuisine")

        # No chunks in the result at all, so there is nothing to reuse a citation from.
        citations = CitationBuilder(session).build(
            RetrievalResult(query="健康的旺角店", claims=[claim])
        )

        assert claim.id in citations.by_claim
        assert citations.by_claim[claim.id].precision == "claim"
