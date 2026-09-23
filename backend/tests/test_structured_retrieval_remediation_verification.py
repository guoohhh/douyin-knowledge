"""Independent verification gaps for Structured Retrieval remediation 9be3760a."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.citation_builder import CitationBuilder
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.retrieval.retriever import HybridRetriever
from tests.test_structured_retrieval import Corpus, _index


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


@pytest.mark.parametrize(
    ("query", "state"),
    [
        ("我想去的店", "want_to_go"),
        ("我想试的店", "want_to_try"),
        ("我想学的店", "want_to_learn"),
    ],
)
def test_every_supported_state_only_query_works_through_ask(
    session: Session, corpus: Corpus, query: str, state: str
) -> None:
    place = corpus.restaurant(
        f"{state}目标店", district="旺角", cuisine="日料", price=80
    )
    corpus.user_state(place, state)

    turn = ConversationManager(session).ask(query)

    assert place.canonical_name in turn.answer.content
    assert turn.answer.generator == "structured"
    assert turn.answer.has_evidence is False
    assert turn.citations == []


def test_extra_cross_source_link_cannot_create_hybrid_relevance_or_provenance(
    session: Session, corpus: Corpus
) -> None:
    """A good evidence row must not make an additional crossed row trustworthy."""
    place = corpus.restaurant(
        "有正常证据的店", district="旺角", cuisine="日料", price=80
    )
    target_claim = session.scalar(
        select(Claim).where(
            Claim.subject_entity_id == place.id,
            Claim.predicate == "cuisine",
        )
    )
    assert target_claim is not None

    donor_source, donor_run = corpus.source("只含防御边界关键词的来源")
    donor_entity = corpus.entity("无关对象")
    donor_claim = corpus.claim(
        donor_entity,
        donor_source,
        donor_run,
        predicate="located_in",
        text="中环",
        evidence_text="火星防御边界关键词",
    )
    donor_evidence = session.scalar(
        select(EvidenceUnit)
        .join(ClaimEvidence, ClaimEvidence.evidence_id == EvidenceUnit.id)
        .where(ClaimEvidence.claim_id == donor_claim.id)
    )
    assert donor_evidence is not None
    corpus.chunk(donor_source, donor_run, "火星防御边界关键词")
    session.add(
        ClaimEvidence(
            claim_id=target_claim.id,
            evidence_id=donor_evidence.id,
            support_role="supports",
        )
    )
    session.flush()
    _index(session)

    result = HybridRetriever(session).retrieve(
        "火星防御边界关键词", limit=10, use_vector=False, include_claims=True
    )
    citations = CitationBuilder(session).build(result)

    assert result.chunks
    assert {chunk.source_id for chunk in result.chunks} == {donor_source.id}
    leaked_into_relevance = target_claim.id in {claim.id for claim in result.claims}
    leaked_into_citations = target_claim.id in citations.by_claim
    assert (leaked_into_relevance, leaked_into_citations) == (False, False)
