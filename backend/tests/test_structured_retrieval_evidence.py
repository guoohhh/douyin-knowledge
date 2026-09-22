"""Rule 6: a claim may only be presented on evidence from its *own* source.

``GroundingValidator.validate`` hard-rejects a claim whose evidence belongs to another
source (``rejected_context``, never persisted), so nothing should ever be written in that
shape. These tests are about the other half of that contract: retrieval must fail closed
if the persisted data is inconsistent anyway -- because a link row was deleted, because a
migration rewrote provenance, or because something wrote around the validator.

Two defects live here, and both are about *rendered* output rather than executor return
values. An entity that qualifies with no citable evidence still reaches the user as a
named, apparently-verified answer; so does an entity cited through evidence from a video
that never mentioned it. Asserting on `result.matches` alone would have missed both, so
every test below goes through the citation builder and the answer generator.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.retrieval.query_parser import parse_query
from tests.test_structured_retrieval import CANONICAL_QUERY, Corpus, _index

# --------------------------------------------------------------------- helpers


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _answer(session: Session, query: str = CANONICAL_QUERY):
    """Render one turn the way `TestRejectionsReachTheAnswer._answer` does.

    No vector store and no chat model, so the text is deterministic and the assertions
    are about the structured path only. Returns the rendered content plus the citation
    set, because "not presented" and "not cited" are two separate failures.
    """
    from douyin_knowledge.conversation.answer_generator import AnswerGenerator
    from douyin_knowledge.conversation.citation_builder import CitationBuilder
    from douyin_knowledge.conversation.scope import classify_scope
    from douyin_knowledge.retrieval.retriever import HybridRetriever

    decision = classify_scope(query)
    plan, _ = parse_query(query, scope=decision.scope, limit=10)
    result = HybridRetriever(session).retrieve_structured(plan, use_vector=False)
    citations = CitationBuilder(session).build(result)
    content = AnswerGenerator().generate(query, result, citations, decision).content
    return content, citations, result


def _ask(session: Session, query: str = CANONICAL_QUERY, **kwargs):
    from douyin_knowledge.conversation.conversation_manager import ConversationManager

    return ConversationManager(session).ask(query, **kwargs)


def _claim_ids_of(session: Session, entity: Entity) -> list[str]:
    return [
        claim.id
        for claim in session.scalars(select(Claim).where(Claim.subject_entity_id == entity.id))
    ]


def _strip_all_evidence(session: Session, entity: Entity) -> list[str]:
    """Delete every ClaimEvidence row for this entity's claims. Returns the claim ids."""
    claim_ids = _claim_ids_of(session, entity)
    session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids)))
    session.flush()
    assert not session.scalars(
        select(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids))
    ).all()
    return claim_ids


# ------------------------------------------------------- P0-1: no evidence at all


class TestAClaimWithNoEvidenceIsNotKnowledge:
    """Deleting every ClaimEvidence row must remove the entity from the answer.

    Before rule 6 the claim still satisfied every hard constraint, so the entity was
    named in the prose with its price and carried a claim-precision citation pointing at
    a source whose link to that claim no longer existed. That is fabricated provenance:
    the citation looks checkable and resolves to nothing.
    """

    def test_the_entity_is_not_presented_as_supported_knowledge(
        self, session: Session, corpus: Corpus
    ) -> None:
        entity = corpus.restaurant(
            "旺角无凭据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角无凭据店 人均80 很地道的日料",
        )
        claim_ids = _strip_all_evidence(session, entity)
        _index(session)

        content, citations, result = _answer(session)

        assert "旺角无凭据店" not in content, (
            "an entity whose claims have no evidence was named as a found result"
        )
        assert [c.claim_id for c in citations.citations if c.claim_id] == []
        assert not (set(claim_ids) & set(citations.by_claim))
        assert result.structured is not None
        assert result.structured.matches == []

    def test_the_turn_through_the_entry_point_cites_nothing_for_it(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Same defect at the product boundary, where the user actually sees it."""
        entity = corpus.restaurant(
            "旺角无凭据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角无凭据店 人均80 很地道的日料",
        )
        claim_ids = _strip_all_evidence(session, entity)
        _index(session)

        body = _ask(session).as_dict()

        assert "旺角无凭据店" not in body["content"]
        for citation in body["citations"]:
            assert citation.get("claim_id") not in claim_ids


# --------------------------------------------- P0-2: evidence from another source


class TestEvidenceFromAnotherSourceCannotSupportAClaim:
    """A claim on source A must not be presented or cited via evidence from source B.

    This is the exact shape ``GroundingValidator.validate`` rejects with
    ``rejected_context``. Retrieval used to accept it: ``_claim_evidence_map`` read
    ``claim_evidence`` without ever comparing source ids, so a crossed row let the claim
    reuse source B's chunk citation -- an answer about restaurant A pointing the user at
    a timestamp in a video about something else entirely.
    """

    def _repoint_to_another_source(
        self, session: Session, entity: Entity, other_source: Source
    ) -> list[str]:
        """Move every link of this entity's claims onto `other_source`'s evidence."""
        other_evidence = session.scalars(
            select(EvidenceUnit).where(EvidenceUnit.source_id == other_source.id)
        ).first()
        assert other_evidence is not None
        claim_ids = _claim_ids_of(session, entity)
        session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids)))
        session.flush()
        for claim_id in claim_ids:
            session.add(
                ClaimEvidence(
                    claim_id=claim_id, evidence_id=other_evidence.id, support_role="supports"
                )
            )
        session.flush()
        return claim_ids

    def test_a_cross_source_claim_does_not_qualify_or_cite(
        self, session: Session, corpus: Corpus
    ) -> None:
        crossed = corpus.restaurant(
            "旺角借证店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角借证店 人均80 很地道的日料",
        )
        donor_source, donor_run = corpus.source("无关视频")
        donor_entity = corpus.entity("无关店")
        corpus.claim(
            donor_entity, donor_source, donor_run, predicate="located_in", text="中环",
            evidence_text="完全无关的一段话",
        )
        corpus.chunk(donor_source, donor_run, "完全无关的一段话")
        claim_ids = self._repoint_to_another_source(session, crossed, donor_source)
        _index(session)

        content, citations, result = _answer(session)

        assert "旺角借证店" not in content, (
            "a claim supported only by another source's evidence was presented as knowledge"
        )
        assert result.structured is not None
        assert result.structured.matches == []
        for citation in citations.citations:
            assert citation.claim_id not in claim_ids
        # The donor's evidence must never appear as this claim's provenance, whether or not
        # the donor itself happens to be cited for its own reasons.
        assert not (set(claim_ids) & set(citations.by_claim))

    def test_a_crossed_link_is_never_used_to_cite_a_claim(
        self, session: Session, corpus: Corpus
    ) -> None:
        """The narrow citation-builder case: same-source evidence *plus* a crossed row.

        The claim stays eligible (rule 6 is satisfied by the good row), so this isolates
        the reuse lookup: the crossed evidence id must not be what the claim's citation is
        resolved through, even when that evidence is already on screen as a chunk citation.
        """
        from douyin_knowledge.conversation.citation_builder import CitationBuilder

        entity = corpus.restaurant(
            "旺角双证店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角双证店 人均80 很地道的日料",
        )
        donor_source, donor_run = corpus.source("无关视频")
        donor_entity = corpus.entity("无关店")
        corpus.claim(
            donor_entity, donor_source, donor_run, predicate="located_in", text="旺角",
            evidence_text="无关视频里的旺角",
        )
        donor_evidence = session.scalars(
            select(EvidenceUnit).where(EvidenceUnit.source_id == donor_source.id)
        ).first()
        assert donor_evidence is not None
        claim_ids = _claim_ids_of(session, entity)
        for claim_id in claim_ids:
            session.add(
                ClaimEvidence(
                    claim_id=claim_id, evidence_id=donor_evidence.id, support_role="supports"
                )
            )
        session.flush()

        crossed = CitationBuilder(session)._claim_evidence_map(claim_ids)
        for claim_id, evidence_ids in crossed.items():
            assert donor_evidence.id not in evidence_ids, (
                f"claim {claim_id} still resolves through evidence from another source"
            )

        content, _, _ = _answer(session)
        assert "旺角双证店" in content, "the good same-source link must still support it"


# ------------------------------------------------- the global provenance invariant


class TestEveryCitationRestsOnItsOwnSourcesEvidence:
    """Walk Claim -> ClaimEvidence -> EvidenceUnit -> Source for every citation, and compare.

    The existing end-to-end test walks this chain but only asserts each hop *resolves*; a
    chain that resolves to the wrong source resolves perfectly well. This asserts the
    comparison the grounding validator makes, over whatever the healthy corpus produced.
    """

    def test_no_citation_crosses_a_source_boundary(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant(
            "旺角松本食堂",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角松本食堂 人均80 很地道的日料",
        )
        corpus.restaurant(
            "旺角二番",
            district="旺角",
            cuisine="日料",
            price=90,
            evidence_text="旺角二番 人均90 日料",
        )
        corpus.restaurant("中环贵价意菜", district="中环", cuisine="意大利菜", price=400)
        _index(session)

        body = _ask(session).as_dict()

        assert body["citations"], "the invariant is vacuous with no citations"
        checked = 0
        for citation in body["citations"]:
            claim_id = citation.get("claim_id")
            if not claim_id:
                continue
            claim = session.get(Claim, claim_id)
            assert claim is not None
            links = session.scalars(
                select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)
            ).all()
            assert links, "a cited claim must have evidence behind it"
            same_source = []
            for link in links:
                evidence = session.get(EvidenceUnit, link.evidence_id)
                assert evidence is not None
                assert session.get(Source, evidence.source_id) is not None
                if evidence.source_id == claim.source_id:
                    same_source.append(evidence)
            assert same_source, (
                f"cited claim {claim.id} on source {claim.source_id} has no evidence "
                "from its own source"
            )
            checked += 1
        assert checked, "no claim citation was checked, so the walk proved nothing"


# ------------------------------------------------------------- healthy control


class TestTheHealthyPathIsUnchanged:
    """Rule 6 must not be over-broad: intact same-source evidence still qualifies and cites."""

    def test_a_normal_corpus_still_answers_with_citations(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant(
            "旺角松本食堂",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角松本食堂 人均80 很地道的日料",
        )
        corpus.restaurant("中环贵价意菜", district="中环", cuisine="意大利菜", price=400)
        _index(session)

        body = _ask(session).as_dict()

        assert body["meta"]["generator"] == "structured"
        assert "旺角松本食堂" in body["content"]
        assert "80" in body["content"]
        assert "中环贵价意菜" not in body["content"]
        assert body["has_evidence"] is True
        assert body["citations"]
        assert body["meta"]["diagnostics"]["structured"]["matched"] == 1

    def test_qualification_still_works_with_no_chunk_at_all(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Evidence exists but nothing was indexed: rule 6 is about links, not about FTS."""
        corpus.restaurant("旺角一番", district="旺角", cuisine="日料", price=80)

        content, citations, result = _answer(session)

        assert result.structured is not None
        assert [m.entity.canonical_name for m in result.structured.matches] == ["旺角一番"]
        assert "旺角一番" in content
        assert citations.citations
