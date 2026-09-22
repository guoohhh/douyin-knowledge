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
from douyin_knowledge.retrieval.query_parser import parse_query
from douyin_knowledge.retrieval.query_plan import (
    MAX_LIMIT,
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


# --------------------------------------------------- operator boundary matrix


class TestOperatorBoundaries:
    """Every whitelisted operator against a value sitting exactly on the threshold.

    100 is the interesting value precisely because it is the one the four inequality
    operators disagree about. A test at 80 passes under ``<`` and ``<=`` alike and would
    not notice the two being swapped.
    """

    @pytest.mark.parametrize(
        ("query", "operator", "at_100_qualifies"),
        [
            ("我收藏过哪些旺角人均100以下的日料？", "<", False),
            ("我收藏过哪些旺角人均不超过100的日料？", "<=", True),
            ("我收藏过哪些旺角人均正好100的日料？", "=", True),
            ("我收藏过哪些旺角人均超过100的日料？", ">", False),
            ("我收藏过哪些旺角人均100以上的日料？", ">=", True),
        ],
    )
    def test_threshold_value(
        self,
        session: Session,
        corpus: Corpus,
        query: str,
        operator: str,
        at_100_qualifies: bool,
    ) -> None:
        corpus.restaurant("旺角百元屋", district="旺角", cuisine="日料", price=100)

        plan = _plan(query)
        constraint = plan.constraint_for("price_per_person")
        assert constraint is not None, f"{query!r} lost its price constraint"
        assert constraint.operator == operator
        assert constraint.operator in NUMERIC_OPERATORS

        result = StructuredExecutor(session).execute(plan)
        assert bool(result.matches) is at_100_qualifies
        if not at_100_qualifies:
            assert "numeric_constraint_failed" in _reasons(result)

    @pytest.mark.parametrize(
        ("price", "expected"),
        [(99.0, True), (99.99, True), (100.0, False), (100.01, False), (101.0, False)],
    )
    def test_strict_less_than_is_exclusive_at_the_boundary(
        self, session: Session, corpus: Corpus, price: float, expected: bool
    ) -> None:
        """`< 100` on both sides of 100, including sub-unit distances.

        ``value_number`` is a float column, so a price recorded as 99.99 must qualify. A
        comparison that rounded or truncated to int would change the answer here and
        nowhere else in the suite.
        """
        corpus.restaurant("旺角边界屋", district="旺角", cuisine="日料", price=price)
        result = StructuredExecutor(session).execute(_plan())
        assert bool(result.matches) is expected


# ----------------------------------------------------- Chinese phrasing matrix


class TestChinesePhrasing:
    """The phrasings the brief names, plus the inversion traps around them."""

    @pytest.mark.parametrize(
        ("phrase", "operator", "value"),
        [
            ("人均100以下", "<", 100.0),
            ("人均不超过100", "<=", 100.0),
            ("人均100以内", "<=", 100.0),
            ("人均100之内", "<=", 100.0),
            ("人均低于100", "<", 100.0),
            ("人均不到100", "<", 100.0),
            ("人均少于100", "<", 100.0),
            ("人均超过100", ">", 100.0),
            ("人均100以上", ">=", 100.0),
            ("人均高于100", ">", 100.0),
            ("人均100起", ">=", 100.0),
            ("人均正好100", "=", 100.0),
            ("人均刚好100", "=", 100.0),
            ("人均等于100", "=", 100.0),
            ("100以内的人均", "<=", 100.0),
            ("人均预算100以内", "<=", 100.0),
        ],
    )
    def test_price_phrasing(self, phrase: str, operator: str, value: float) -> None:
        plan = _plan(f"我收藏过哪些旺角{phrase}的日料？")
        constraint = plan.constraint_for("price_per_person")
        assert constraint is not None, f"{phrase!r} produced no price constraint"
        assert (constraint.operator, constraint.value_number) == (operator, value)

    def test_negated_markers_are_not_read_as_their_positive_form(self) -> None:
        """不超过 must not match as 超过.

        Regex alternation is first-match rather than longest-match, so a marker list that
        happens to put 超过 before 不超过 inverts the comparison silently -- the query still
        parses, still returns results, and every one of them is wrong.
        """
        assert _plan("人均不超过100的日料").constraint_for("price_per_person").operator == "<="
        assert _plan("人均超过100的日料").constraint_for("price_per_person").operator == ">"

    @pytest.mark.parametrize("query", ["便宜的日料", "人均大概80的日料", "有什么好吃的"])
    def test_unsupported_phrasing_yields_no_constraint_rather_than_a_guess(
        self, query: str
    ) -> None:
        """A vague quantity produces no numeric constraint at all.

        "人均大概80" is a hedge, not a threshold, and inventing ``<= 80`` from it would
        silently drop an 85 the user would have accepted. Under-matching is recoverable by
        rephrasing; a fabricated constraint is not visible to the user at all.
        """
        plan = _plan(query)
        assert plan.constraint_for("price_per_person") is None


# ------------------------------------------------------- malformed plan matrix


class TestMalformedPlans:
    """A model-proposed plan is untrusted input. Every rejection is by code.

    Asserting the *code* rather than just "it raised" is what makes these tests useful: a
    plan rejected for the wrong reason still reaches the user as a generic failure, and the
    codes are what the diagnostics surface so a bad parser can be told from a bad query.
    """

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("not a dict", "plan_not_object"),
            ([], "plan_not_object"),
            ({"intent": "delete_everything"}, "unsupported_intent"),
            ({"intent": 7}, "unsupported_intent"),
            ({"entity_types": {"place": True}}, "bad_entity_types"),
            ({"entity_types": [1]}, "bad_entity_types"),
            ({"entity_types": ["restaurant"]}, "unsupported_entity_type"),
            ({"location": "旺角"}, "bad_location"),
            ({"location": {"district": 1}}, "bad_location"),
            ({"location": {"district": "旺角", "city": 1}}, "bad_location"),
            ({"location": {"district": "火星"}}, "unknown_district"),
            ({"claim_constraints": {"field": "cuisine"}}, "bad_constraints"),
            ({"claim_constraints": ["cuisine"]}, "constraint_not_object"),
            ({"claim_constraints": [{"field": "rating"}]}, "unsupported_field"),
            ({"claim_constraints": [{"field": None}]}, "unsupported_field"),
            ({"limit": 0}, "bad_limit"),
            ({"limit": -1}, "bad_limit"),
            ({"limit": "10"}, "bad_limit"),
            ({"limit": True}, "bad_limit"),
            ({"parser": ""}, "bad_parser"),
            ({"user_state": "visited"}, "bad_user_state"),
            ({"user_state": {"visited": "yes"}}, "bad_user_state"),
        ],
    )
    def test_rejected_with_code(self, raw: object, code: str) -> None:
        with pytest.raises(QueryPlanError) as excinfo:
            validate_plan(raw, raw_query="q", scope="personal_first")
        assert excinfo.value.code == code

    @pytest.mark.parametrize(
        ("operator", "code"),
        [
            ("LIKE", "unsupported_operator"),
            ("!=", "unsupported_operator"),
            ("BETWEEN", "unsupported_operator"),
            ("; DROP TABLE claims; --", "unsupported_operator"),
            ("<>", "unsupported_operator"),
        ],
    )
    def test_operators_outside_the_whitelist_never_reach_sql(
        self, operator: str, code: str
    ) -> None:
        """An unwhitelisted operator is rejected as a value, not escaped as a fragment.

        This is the boundary that makes "no model-generated SQL" true rather than aspirational:
        the operator is matched against a frozenset and used to pick a Python comparison, so a
        SQL fragment in this position can only ever be an unrecognised string.
        """
        with pytest.raises(QueryPlanError) as excinfo:
            validate_plan(
                {
                    "claim_constraints": [
                        {"field": "price_per_person", "operator": operator, "value": 100}
                    ]
                },
                raw_query="q",
                scope="personal_first",
            )
        assert excinfo.value.code == code

    @pytest.mark.parametrize("operator", sorted(NUMERIC_OPERATORS))
    def test_text_fields_reject_numeric_operators(self, operator: str) -> None:
        """Only ``=`` is meaningful on a cuisine. ``cuisine < 日料`` has no semantics."""
        raw = {"claim_constraints": [{"field": "cuisine", "operator": operator, "value": "日料"}]}
        if operator == "=":
            assert validate_plan(raw, raw_query="q", scope="personal_first").claim_constraints
            return
        with pytest.raises(QueryPlanError) as excinfo:
            validate_plan(raw, raw_query="q", scope="personal_first")
        assert excinfo.value.code == "unsupported_operator"

    @pytest.mark.parametrize(
        "value", [None, "100", True, False, float("nan"), float("inf"), float("-inf"), [100]]
    )
    def test_numeric_fields_reject_non_finite_and_non_numeric_values(
        self, value: object
    ) -> None:
        """``True`` and ``NaN`` are the two that slip through a naive isinstance check.

        ``bool`` is a subclass of ``int``, so ``price < True`` would validate and compare
        against 1. ``NaN`` compares false against everything, which would turn a constraint
        into a silent "exclude all" rather than an error.
        """
        with pytest.raises(QueryPlanError) as excinfo:
            validate_plan(
                {"claim_constraints": [{"field": "price_per_person", "value": value}]},
                raw_query="q",
                scope="personal_first",
            )
        assert excinfo.value.code == "bad_value_type"

    def test_duplicate_constraints_on_one_field_are_rejected(self) -> None:
        """Two bounds on one field would need conjunction the executor does not implement."""
        with pytest.raises(QueryPlanError) as excinfo:
            validate_plan(
                {
                    "claim_constraints": [
                        {"field": "price_per_person", "operator": ">", "value": 50},
                        {"field": "price_per_person", "operator": "<", "value": 100},
                    ]
                },
                raw_query="q",
                scope="personal_first",
            )
        assert excinfo.value.code == "duplicate_constraint"

    def test_a_model_cannot_downgrade_the_scope(self) -> None:
        """`scope` is a parameter, never read from the proposed plan.

        A model that answered 我收藏过 with ``scope: general`` would license answering from
        world knowledge, which is the one failure the scope classifier exists to prevent.
        """
        plan = validate_plan(
            {"scope": "general", "intent": "find"},
            raw_query=CANONICAL_QUERY,
            scope="personal_required",
        )
        assert plan.scope == "personal_required"

    def test_limit_is_capped_rather_than_rejected(self) -> None:
        """An over-large limit is bounded, because the executor's cost must be bounded."""
        plan = validate_plan({"limit": 10_000}, raw_query="q", scope="personal_first")
        assert plan.limit == MAX_LIMIT

    def test_a_malformed_plan_never_escapes_parse_query(self) -> None:
        """`parse_query` degrades to an unstructured plan; it does not raise into the turn.

        A parser failure must not take down the conversation. The plan comes back
        unstructured, the query falls through to ordinary retrieval, and the reason is left
        in the diagnostics rather than in a traceback.
        """
        plan, diagnostics = parse_query("!!!", scope="personal_required")
        assert plan.scope == "personal_required"
        assert not plan.is_structured
        assert diagnostics["parser_attempts"]


# ------------------------------------------- the two eligibility implementations


class TestEligibilityImplementationsAgree:
    """`apply_claim_eligibility` (SQL) must select exactly what `eligible_claims` (Python) does.

    `apply_claim_eligibility`'s own docstring promises this test. The two exist because the
    Python filter loads every row its statement matches -- fine for one entity's claims,
    not for a predicate-wide scan -- and two implementations of one rule drift unless
    something pins them together.
    """

    @pytest.fixture
    def all_five_rules(self, corpus: Corpus) -> dict[str, str]:
        """One claim per eligibility rule, plus one that satisfies all five."""
        ids: dict[str, str] = {}

        entity = corpus.entity("合格屋")
        source, run = corpus.source("合格屋 探店")
        ids["eligible"] = corpus.claim(
            entity, source, run, predicate="price_per_person", number=80
        ).id

        # Rule 1: source locally deleted.
        deleted_entity = corpus.entity("已删屋")
        deleted_source, deleted_run = corpus.source("已删屋 探店", deleted=True)
        ids["deleted"] = corpus.claim(
            deleted_entity, deleted_source, deleted_run,
            predicate="price_per_person", number=80,
        ).id

        # Rule 2: no current run pointer (DB-004).
        pending_entity = corpus.entity("待处理屋")
        pending_source, pending_run = corpus.source("待处理屋 探店", current=False)
        ids["no_current_run"] = corpus.claim(
            pending_entity, pending_source, pending_run,
            predicate="price_per_person", number=80,
        ).id

        # Rule 3: hidden policy action.
        for action in ("exclude", "metadata_only"):
            hidden_entity = corpus.entity(f"{action}屋")
            hidden_source, hidden_run = corpus.source(
                f"{action}屋 探店", policy_action=action
            )
            ids[action] = corpus.claim(
                hidden_entity, hidden_source, hidden_run,
                predicate="price_per_person", number=80,
            ).id

        # Rule 4: claim belongs to a superseded run.
        old_entity = corpus.entity("旧结果屋")
        old_source, old_run = corpus.source("旧结果屋 探店")
        ids["superseded"] = corpus.claim(
            old_entity, old_source, old_run, predicate="price_per_person", number=80
        ).id
        corpus.supersede(old_source, corpus.run(old_source))

        # Rule 5: not assertable (DEC-017).
        for status in ("downgraded", "rejected_span"):
            ungrounded_entity = corpus.entity(f"{status}屋")
            ungrounded_source, ungrounded_run = corpus.source(f"{status}屋 探店")
            ids[status] = corpus.claim(
                ungrounded_entity, ungrounded_source, ungrounded_run,
                predicate="price_per_person", number=80, grounding_status=status,
            ).id

        # `None` is grandfathered for claims written before migration 0005.
        legacy_entity = corpus.entity("旧版屋")
        legacy_source, legacy_run = corpus.source("旧版屋 探店")
        ids["legacy_null_grounding"] = corpus.claim(
            legacy_entity, legacy_source, legacy_run,
            predicate="price_per_person", number=80, grounding_status=None,
        ).id

        corpus.session.flush()
        return ids

    def test_same_claim_ids(self, session: Session, all_five_rules: dict[str, str]) -> None:
        base = select(Claim).where(Claim.predicate == "price_per_person")
        via_python = {c.id for c in eligible_claims(session, base)}
        via_sql = {c.id for c in session.scalars(apply_claim_eligibility(base))}
        assert via_python == via_sql

    def test_each_rule_actually_excludes(
        self, session: Session, all_five_rules: dict[str, str]
    ) -> None:
        """The agreement above is only meaningful if the fixture exercises every rule.

        Two implementations that both return everything would agree perfectly, so this
        pins down which ids must be absent -- otherwise a dropped rule passes both tests.
        """
        base = select(Claim).where(Claim.predicate == "price_per_person")
        selected = {c.id for c in session.scalars(apply_claim_eligibility(base))}

        assert all_five_rules["eligible"] in selected
        assert all_five_rules["legacy_null_grounding"] in selected
        for key in (
            "deleted",
            "no_current_run",
            "exclude",
            "metadata_only",
            "superseded",
            "downgraded",
            "rejected_span",
        ):
            assert all_five_rules[key] not in selected, f"{key} must not be eligible"

    def test_the_executor_uses_the_shared_rule(
        self, session: Session, corpus: Corpus
    ) -> None:
        """A hidden source's perfectly-matching claim must not qualify.

        This is the leak `eligibility.py`'s docstring warns about: a surface that applies
        some of the rules looks correct on the happy path and silently answers from
        knowledge the user excluded.
        """
        corpus.restaurant(
            "旺角隐藏屋", district="旺角", cuisine="日料", price=80, policy_action="exclude"
        )
        result = StructuredExecutor(session).execute(_plan())
        assert result.matches == []


# --------------------------------------- the real extraction path writes the claims


class TestExtractionWritesStructuredClaims:
    """The fixtures above assume ``located_in``/``cuisine`` claims exist. This proves they do.

    Everything else in this module builds claims directly, which would keep passing if the
    extraction path stopped writing them entirely -- the executor would simply find nothing
    in production while every test stayed green. This class closes that gap by running the
    real heuristics and the real grounding validator over subtitle-shaped text.
    """

    @pytest.fixture
    def extracted(self, session: Session, corpus: Corpus) -> list[Claim]:
        from douyin_knowledge.ai.adapters.mock_adapter import MockStructuredModel
        from douyin_knowledge.extraction.claim_extractor import ClaimExtractor
        from douyin_knowledge.extraction.entity_extractor import EntityExtractor
        from douyin_knowledge.extraction.entity_resolver import EntityResolver

        source, run = corpus.source("旺角日料 探店")
        # The name must be one the entity heuristics can actually see: `_NAME` excludes
        # common function characters, so 山下食堂 would match nothing because of 下.
        text = "今天来旺角这家松本食堂，人均80块，很地道的日料。"
        evidence = EvidenceUnit(
            source_id=source.id,
            kind="transcript",
            raw_text=text,
            normalized_text=text,
            content_hash="h_extraction_wanchai_japanese",
        )
        session.add(evidence)
        session.flush()

        mentions = EntityExtractor(MockStructuredModel()).extract_from_evidence(
            session, [evidence], source_id=source.id, processing_run_id=run.id
        )
        # Resolution is a separate step in the real pipeline, and it is not optional here:
        # an unresolved mention gives the claim no entity subject, and the executor filters
        # on `subject_entity_id`. Skipping it would have this fixture write claims that no
        # structured query could ever reach.
        resolver = EntityResolver()
        for mention in mentions:
            resolver.resolve_mention(session, mention)
        session.flush()

        claims = ClaimExtractor(MockStructuredModel()).extract_from_evidence(
            session,
            [evidence],
            source_id=source.id,
            processing_run_id=run.id,
            mentions=mentions,
            creator_name="阿明",
        )
        session.flush()
        return claims

    def test_district_and_cuisine_claims_are_written(self, extracted: list[Claim]) -> None:
        by_predicate = {c.predicate: c for c in extracted}
        assert "located_in" in by_predicate, (
            f"no district claim; got {sorted({c.predicate for c in extracted})}"
        )
        assert "cuisine" in by_predicate
        assert "price_per_person" in by_predicate

    def test_values_are_canonical_not_the_matched_surface_form(
        self, extracted: list[Claim]
    ) -> None:
        """Normalizing on write is what lets the executor use exact equality at query time.

        If the writer stored whatever surface form appeared in the subtitle, the executor
        would need fuzzy matching -- and fuzzy matching on a district is how a Mong Kok
        query starts returning Central restaurants.
        """
        from douyin_knowledge.retrieval.vocabulary import canonical_cuisine, canonical_district

        for claim in extracted:
            if claim.predicate == "located_in":
                assert claim.value_text == canonical_district(claim.value_text or "")
            if claim.predicate == "cuisine":
                assert claim.value_text == canonical_cuisine(claim.value_text or "")

    def test_extracted_claims_are_assertable_and_cited(
        self, session: Session, extracted: list[Claim]
    ) -> None:
        """Canonical values still have to pass grounding.

        A canonical value need not appear literally in the evidence -- the creator said
        日料 and the vocabulary may canonicalize something else to it -- so grounding checks
        for *any* surface form of the canonical value instead of skipping the check. A
        claim that failed it would not be assertable and could never answer a query.
        """
        structured = [c for c in extracted if c.predicate in {"located_in", "cuisine"}]
        assert structured
        for claim in structured:
            assert claim.grounding_status in {None, "valid"}, claim.grounding_status
            links = session.scalars(
                select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)
            ).all()
            assert links, f"{claim.predicate} claim has no evidence link"

    def test_the_canonical_query_finds_what_extraction_wrote(
        self, session: Session, extracted: list[Claim]
    ) -> None:
        """End to end on real extraction output: no hand-built claims anywhere in this path."""
        result = StructuredExecutor(session).execute(_plan())

        assert result.matches, (
            "extraction wrote the claims but the executor did not match them; "
            f"diagnostics={result.diagnostics}"
        )
        match = result.matches[0]
        assert set(match.supports) == {"district", "cuisine", "price_per_person"}
        assert match.source_ids
