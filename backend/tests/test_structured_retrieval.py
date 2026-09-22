"""Structured Retrieval V1: hard constraints beat similarity, and prove why.

The eight cases the phase brief requires all live here, plus the operator boundary
matrix, the malformed-plan matrix, and the Chinese phrasing matrix.

Two things about the fixtures are deliberate.

*They are hand-built, not orchestrator-driven.* Cases 6 and 7 need a ``metadata_only``
source and a superseded run respectively, and driving the real pipeline into those states
takes a policy change plus a reprocess -- which is what ``test_policy_visibility.py`` and
``test_processing_currency.py`` already cover. Here the interesting variable is what the
*executor* does with those states, so they are set directly and precisely.
``TestExtractionWritesStructuredClaims`` covers the other half: that the real extraction
path actually produces the ``located_in``/``cuisine`` claims these fixtures assume.

*Every claim gets real evidence.* A claim with no ``ClaimEvidence`` row would still
satisfy a constraint but could not be cited, and an uncitable match is exactly the
"fabricated provenance" the brief forbids. Building them properly means the citation
assertions are meaningful rather than vacuous.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.knowledge.eligibility import (
    apply_claim_eligibility,
    eligible_claims,
)
from douyin_knowledge.retrieval.query_parser import parse_query, propose_plan_deterministic
from douyin_knowledge.retrieval.query_plan import (
    NUMERIC_OPERATORS,
    QueryPlanError,
    validate_plan,
)
from douyin_knowledge.retrieval.structured import StructuredExecutor

CANONICAL_QUERY = "我收藏过哪些旺角人均100以下的日料？"


# --------------------------------------------------------------------- builders


class Corpus:
    """Minimal builder for the Entity/Claim/Evidence/Source spine."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._n = 0

    def source(
        self,
        name: str,
        *,
        policy_action: str = "process",
        current: bool = True,
        deleted: bool = False,
    ) -> tuple[Source, ProcessingRun]:
        self._n += 1
        source = Source(
            platform="douyin",
            external_id=f"s_struct_{self._n}",
            source_type="video",
            title=name,
            locally_deleted_at_ms=now_ms() if deleted else None,
        )
        self.session.add(source)
        self.session.flush()

        run = self.run(source)
        self.session.add(
            SourceProcessingState(
                source_id=source.id,
                current_policy_action=policy_action,
                processing_status="succeeded",
                current_processing_run_id=run.id if current else None,
            )
        )
        self.session.flush()
        return source, run

    def run(self, source: Source) -> ProcessingRun:
        run = ProcessingRun(
            source_id=source.id,
            run_kind="full",
            schema_version="1.0",
            processor_version="0.1.0",
            status="succeeded",
            target_level=2,
            achieved_level=2,
            started_at_ms=now_ms(),
            finished_at_ms=now_ms(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def supersede(self, source: Source, run: ProcessingRun) -> None:
        state = self.session.get(SourceProcessingState, source.id)
        assert state is not None
        state.current_processing_run_id = run.id
        self.session.flush()

    def entity(self, name: str, *, entity_type: str = "place") -> Entity:
        row = Entity(
            entity_type=entity_type,
            subtype="restaurant" if entity_type == "place" else None,
            canonical_name=name,
            normalized_name=name.casefold(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim(
        self,
        entity: Entity,
        source: Source,
        run: ProcessingRun,
        *,
        predicate: str,
        number: float | None = None,
        text: str | None = None,
        attribution: str = "阿明",
        grounding_status: str | None = "valid",
        evidence_text: str | None = None,
    ) -> Claim:
        evidence = EvidenceUnit(
            source_id=source.id,
            kind="transcript",
            raw_text=evidence_text or f"{entity.canonical_name} {text or number}",
            normalized_text=evidence_text or f"{entity.canonical_name} {text or number}",
            # Run id is part of the hash: case 7 puts two runs of the same source in the
            # same table, and a hash without it collides on the unique index.
            content_hash=f"h_{run.id}_{predicate}_{text or number}_{attribution}",
        )
        self.session.add(evidence)
        self.session.flush()

        row = Claim(
            source_id=source.id,
            processing_run_id=run.id,
            subject_entity_id=entity.id,
            predicate=predicate,
            value_type="number" if number is not None else "text",
            value_number=number,
            value_text=text,
            unit="per_person" if predicate == "price_per_person" else None,
            currency="CNY" if predicate == "price_per_person" else None,
            claim_kind="measurement" if number is not None else "attribute",
            provenance_type="creator_statement",
            attribution=attribution,
            confidence=0.85,
            grounding_status=grounding_status,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(
            ClaimEvidence(claim_id=row.id, evidence_id=evidence.id, support_role="supports")
        )
        self.session.flush()
        return row

    def restaurant(
        self,
        name: str,
        *,
        district: str,
        cuisine: str,
        price: float | None,
        attribution: str = "阿明",
        policy_action: str = "process",
        grounding_status: str | None = "valid",
    ) -> Entity:
        """One fully-described restaurant on one source."""
        source, run = self.source(f"{name} 探店", policy_action=policy_action)
        entity = self.entity(name)
        self.claim(
            entity, source, run, predicate="located_in", text=district,
            attribution=attribution, grounding_status=grounding_status,
        )
        self.claim(
            entity, source, run, predicate="cuisine", text=cuisine,
            attribution=attribution, grounding_status=grounding_status,
        )
        if price is not None:
            self.claim(
                entity, source, run, predicate="price_per_person", number=price,
                attribution=attribution, grounding_status=grounding_status,
            )
        return entity


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _plan(query: str = CANONICAL_QUERY, *, scope: str = "personal_required", limit: int = 10):
    plan, _ = parse_query(query, scope=scope, limit=limit)
    return plan


def _names(result) -> list[str]:
    return [m.entity.canonical_name for m in result.matches]


def _reasons(result) -> set[str]:
    return {r.reason for r in result.rejections}


# ------------------------------------------------- the eight required cases


class TestRequiredRegressionCases:
    """The canonical query against each of the eight fixture shapes in the brief."""

    def test_case_1_matching_restaurant_is_included(
        self, session: Session, corpus: Corpus
    ) -> None:
        """旺角 / 日料 / 80 qualifies, and every hard condition is cited."""
        corpus.restaurant("旺角一番", district="旺角", cuisine="日料", price=80)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角一番"]
        match = result.matches[0]
        # All three hard conditions carry their own supporting claim: that is what makes
        # "why this district / why this cuisine / which price" answerable from the result.
        assert set(match.supports) == {"district", "cuisine", "price_per_person"}
        assert match.supports["price_per_person"].satisfied_by[0].value_number == 80.0
        assert not match.conflicts
        assert match.source_ids, "a qualifying match with no source is unciteable"

    def test_case_2_price_above_threshold_is_excluded(
        self, session: Session, corpus: Corpus
    ) -> None:
        """旺角 / 日料 / 150 fails the numeric constraint and says so."""
        corpus.restaurant("旺角贵一番", district="旺角", cuisine="日料", price=150)

        result = StructuredExecutor(session).execute(_plan())

        assert result.matches == []
        assert "numeric_constraint_failed" in _reasons(result)
        rejection = next(
            r for r in result.rejections if r.reason == "numeric_constraint_failed"
        )
        assert rejection.field == "price_per_person"
        assert "150" in rejection.detail

    def test_case_3_wrong_cuisine_is_excluded(self, session: Session, corpus: Corpus) -> None:
        """旺角 / 港式 / 70 is cheap enough and in the right place, and still excluded."""
        corpus.restaurant("旺角茶记", district="旺角", cuisine="港式", price=70)

        result = StructuredExecutor(session).execute(_plan())

        assert result.matches == []
        assert _reasons(result) & {"text_constraint_failed", "missing_required_claim"}

    def test_case_4_wrong_district_is_excluded(self, session: Session, corpus: Corpus) -> None:
        """中环 / 日料 / 80 satisfies two of three conditions. Two is not enough."""
        corpus.restaurant("中环一番", district="中环", cuisine="日料", price=80)

        result = StructuredExecutor(session).execute(_plan())

        assert result.matches == []
        assert _reasons(result) & {"text_constraint_failed", "missing_required_claim"}

    def test_case_5_conflicting_prices_stay_visible(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Two current eligible sources say 80 and 120. Neither is erased.

        The entity qualifies because eligible evidence satisfies ``< 100``, but the
        answer must not imply a settled price -- Claim != objective fact (KM-003).
        """
        source_a, run_a = corpus.source("A 探店")
        source_b, run_b = corpus.source("B 探店")
        entity = corpus.entity("旺角双价一番")
        for source, run, attribution in (
            (source_a, run_a, "阿明"),
            (source_b, run_b, "小美"),
        ):
            corpus.claim(
                entity, source, run, predicate="located_in", text="旺角",
                attribution=attribution,
            )
            corpus.claim(
                entity, source, run, predicate="cuisine", text="日料",
                attribution=attribution,
            )
        corpus.claim(
            entity, source_a, run_a, predicate="price_per_person", number=80,
            attribution="阿明",
        )
        corpus.claim(
            entity, source_b, run_b, predicate="price_per_person", number=120,
            attribution="小美",
        )

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角双价一番"]
        support = result.matches[0].supports["price_per_person"]
        assert [c.value_number for c in support.satisfied_by] == [80.0]
        assert support.has_conflict
        assert sorted(support.conflicting_values) == [80.0, 120.0]
        # Both claims must reach the citation layer, or the answer cannot show the
        # disagreement it is required to show.
        cited = {c.value_number for c in result.matches[0].claims}
        assert {80.0, 120.0} <= cited

    def test_case_6_metadata_only_source_does_not_qualify(
        self, session: Session, corpus: Corpus
    ) -> None:
        """A source kept as metadata was never understood, so it is not knowledge."""
        corpus.restaurant(
            "旺角未处理一番",
            district="旺角",
            cuisine="日料",
            price=80,
            policy_action="metadata_only",
        )

        result = StructuredExecutor(session).execute(_plan())

        assert result.matches == []
        # The rejection must not read as "no such restaurant": the user has the video.
        assert _reasons(result) == {"no_eligible_claims_metadata_only"}

    def test_case_7_superseded_claim_does_not_qualify(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Old run said 80, current run says 130. ``< 100`` must not match the old 80.

        This is the failure mode with the least visible symptom: the answer looks right,
        cites a real video, and reports a price the archive no longer asserts.
        """
        source, old_run = corpus.source("涨价前后")
        entity = corpus.entity("旺角涨价一番")
        corpus.claim(entity, source, old_run, predicate="located_in", text="旺角")
        corpus.claim(entity, source, old_run, predicate="cuisine", text="日料")
        corpus.claim(entity, source, old_run, predicate="price_per_person", number=80)

        new_run = corpus.run(source)
        corpus.supersede(source, new_run)
        corpus.claim(entity, source, new_run, predicate="located_in", text="旺角")
        corpus.claim(entity, source, new_run, predicate="cuisine", text="日料")
        corpus.claim(entity, source, new_run, predicate="price_per_person", number=130)

        result = StructuredExecutor(session).execute(_plan())

        assert result.matches == []
        assert "numeric_constraint_failed" in _reasons(result)
        detail = " ".join(r.detail for r in result.rejections)
        assert "130" in detail and "80" not in detail, "superseded value must not surface"

    def test_case_8_no_qualifying_evidence_is_an_honest_no_result(
        self, session: Session, corpus: Corpus
    ) -> None:
        """An empty archive answers "not in your collection", not from world knowledge."""
        result = StructuredExecutor(session).execute(_plan())

        assert result.is_empty()
        assert result.rejections == []
        assert result.diagnostics["reason"] in {
            "no_structured_candidates",
            "all_candidates_rejected",
        }
        assert result.plan.scope == "personal_required"

