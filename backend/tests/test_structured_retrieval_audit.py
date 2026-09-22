"""Adversarial regressions for independently confirmed Structured Retrieval defects.

These tests intentionally fail against reviewed integration SHA
83d2d0c623e6ca917914c232b24b93b09f3c01c6.  They are audit evidence, not fixes.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from douyin_knowledge.ai.providers import StructuredResponse
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit
from tests.test_structured_retrieval import Corpus


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _ask(session: Session, query: str, **kwargs: object) -> dict[str, object]:
    return ConversationManager(session).ask(query, **kwargs).as_dict()


class TestParserMustNotInvertOrDropExplicitConditions:
    def test_negated_cuisine_is_not_executed_as_positive_cuisine(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("旺角日料店", district="旺角", cuisine="日料", price=80)
        corpus.restaurant("旺角韩料店", district="旺角", cuisine="韩料", price=80)

        body = _ask(session, "我收藏过哪些不是日料的旺角餐厅？")

        assert "旺角日料店" not in str(body["content"])

    def test_approximate_price_is_not_silently_dropped_from_a_structured_query(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("旺角八十元店", district="旺角", cuisine="日料", price=80)
        corpus.restaurant("旺角三百元店", district="旺角", cuisine="日料", price=300)

        body = _ask(session, "我收藏过哪些旺角人均大概80的日料？")

        assert "旺角三百元店" not in str(body["content"])

    def test_conflicting_price_conditions_do_not_execute_as_one_condition(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("旺角一百五十元店", district="旺角", cuisine="日料", price=150)

        body = _ask(session, "我收藏过哪些旺角人均100以下和200以上的日料？")

        structured = body["meta"]["diagnostics"]["structured"]  # type: ignore[index]
        assert structured["matches"] == []

    def test_invalid_model_location_does_not_degrade_to_cuisine_only(
        self, session: Session, corpus: Corpus
    ) -> None:
        class ProposalModel:
            def extract(
                self,
                prompt: str,
                schema: dict[str, object],
                *,
                temperature: float = 0.0,
            ) -> StructuredResponse:
                del prompt, schema, temperature
                return StructuredResponse(
                    data={"district": "九龙城", "cuisine": "日料"}, model="audit"
                )

        corpus.restaurant("旺角日料店", district="旺角", cuisine="日料", price=80)
        body = ConversationManager(session, structured_model=ProposalModel()).ask(
            "我收藏过哪些九龙城的日本餐厅？"
        ).as_dict()

        assert "旺角日料店" not in str(body["content"])


class TestSourceRestrictionContract:
    def test_state_only_plan_does_not_leak_disallowed_entity_in_api_diagnostics(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("受限来源里的想去店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")

        body = _ask(session, "我想去的店", source_ids=[])

        assert "受限来源里的想去店" not in str(body)

    def test_state_only_plan_does_not_leak_entity_supported_only_by_hidden_source(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("隐藏来源里的想去店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")
        claim = session.scalar(select(Claim).where(Claim.subject_entity_id == place.id))
        assert claim is not None
        state = session.get(SourceProcessingState, claim.source_id)
        assert state is not None
        state.current_policy_action = "exclude"
        session.flush()

        body = _ask(session, "我想去的店")

        assert "隐藏来源里的想去店" not in str(body)


class TestMergedEntityIsNotReturnedAsCurrent:
    def test_claim_candidates_follow_the_entity_merge_pointer(
        self, session: Session, corpus: Corpus
    ) -> None:
        merged = corpus.restaurant("已合并的旧实体", district="旺角", cuisine="日料", price=80)
        survivor = corpus.entity("合并后的当前实体")
        merged.status = "merged"
        merged.merged_into_entity_id = survivor.id
        session.flush()

        body = _ask(session, "我收藏过哪些旺角人均100以下的日料？")

        assert "已合并的旧实体" not in str(body["content"])
        assert "合并后的当前实体" in str(body["content"])


class TestStructuredCitationIntegrity:
    def test_a_structured_match_without_evidence_is_not_presented_as_cited(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("无证据旺角店", district="旺角", cuisine="日料", price=80)
        claim_ids = list(
            session.scalars(select(Claim.id).where(Claim.subject_entity_id == place.id))
        )
        session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids)))
        session.flush()

        body = _ask(session, "我收藏过哪些旺角人均100以下的日料？")

        assert "无证据旺角店" not in str(body["content"])
        assert body["citations"] == []

    def test_claim_citation_cannot_resolve_to_evidence_from_another_source(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant(
            "跨来源证据店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="跨来源证据店 旺角 日料 人均80",
        )
        claims = list(
            session.scalars(select(Claim).where(Claim.subject_entity_id == place.id))
        )
        price_claim = next(c for c in claims if c.predicate == "price_per_person")
        claim_source = session.get(Source, price_claim.source_id)
        assert claim_source is not None

        other_source, other_run = corpus.source("不相关的第二来源")
        other_claim = corpus.claim(
            place,
            other_source,
            other_run,
            predicate="cuisine",
            text="日料",
            evidence_text="跨来源证据店 旺角 日料 人均80",
        )
        other_evidence = session.scalar(
            select(EvidenceUnit)
            .join(ClaimEvidence, ClaimEvidence.evidence_id == EvidenceUnit.id)
            .where(ClaimEvidence.claim_id == other_claim.id)
        )
        assert other_evidence is not None
        corpus.chunk(other_source, other_run, "跨来源证据店 旺角 日料 人均80")

        session.execute(delete(ClaimEvidence).where(ClaimEvidence.claim_id == price_claim.id))
        session.add(
            ClaimEvidence(
                claim_id=price_claim.id,
                evidence_id=other_evidence.id,
                support_role="supports",
            )
        )
        session.flush()

        from douyin_knowledge.search.indexer import reindex_all

        reindex_all(session)
        body = _ask(session, "我收藏过哪些旺角人均100以下的日料？")
        price_marker = re.search(r"人均：80（来自 阿明）\[(\d+)\]", str(body["content"]))
        assert price_marker is not None
        price_citation = next(
            citation
            for citation in body["citations"]  # type: ignore[union-attr]
            if citation["ordinal"] == int(price_marker.group(1))
        )

        assert price_citation["source_id"] == claim_source.id
        assert price_citation["evidence_id"] is not None
        evidence = session.get(EvidenceUnit, price_citation["evidence_id"])
        assert evidence is not None
        assert evidence.source_id == claim_source.id


class TestCandidateCapOrdering:
    def test_candidate_cap_does_not_prefer_database_insertion_order(
        self, session: Session, corpus: Corpus
    ) -> None:
        for index in range(500):
            corpus.restaurant(
                f"Z{index:03d}旺角店", district="旺角", cuisine="日料", price=80
            )
        corpus.restaurant("A应当先返回的旺角店", district="旺角", cuisine="日料", price=80)

        body = _ask(
            session,
            "我收藏过哪些旺角人均100以下的日料？",
            limit=1,
        )

        assert "1. A应当先返回的旺角店" in str(body["content"])


class TestSourceLevelScoresStayWithTheRightEntity:
    def test_one_source_discussing_two_entities_does_not_give_both_the_same_soft_score(
        self, session: Session, corpus: Corpus
    ) -> None:
        source, run = corpus.source("同一条视频里的两家店")
        noisy = corpus.entity("A吵闹店")
        suitable = corpus.entity("Z约会店")
        for entity, attribution, evidence_text in (
            (noisy, "甲", "A吵闹店 很吵 不适合约会"),
            (suitable, "乙", "Z约会店 安静 很适合约会"),
        ):
            corpus.claim(
                entity,
                source,
                run,
                predicate="located_in",
                text="旺角",
                attribution=attribution,
                evidence_text=evidence_text,
            )
            corpus.claim(
                entity,
                source,
                run,
                predicate="cuisine",
                text="日料",
                attribution=attribution,
                evidence_text=evidence_text,
            )
            corpus.claim(
                entity,
                source,
                run,
                predicate="price_per_person",
                number=80,
                attribution=attribution,
                evidence_text=evidence_text,
            )
        corpus.chunk(source, run, "Z约会店 安静 很适合约会")

        from douyin_knowledge.search.indexer import reindex_all

        reindex_all(session)
        body = _ask(
            session,
            "我收藏过哪些旺角人均100以下适合约会的日料？",
            limit=1,
        )

        assert "1. Z约会店" in str(body["content"])
        assert "1. A吵闹店" not in str(body["content"])
