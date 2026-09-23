"""Conditions V1 cannot express are refused, not approximated.

The parser's two failure modes are not symmetrical. Failing to *notice* a condition is
recoverable -- the plan carries one fewer filter and the answer is broader than asked.
Failing to notice a condition that *reverses or splits* an existing one is not: the plan
then carries a filter the user never asked for, and the answer is confidently wrong.

`不是日料` is the first shape. Cuisine is matched by bare substring scan, so the negated
phrase produced `cuisine = 日料` -- precisely the restaurants the user ruled out, presented
as their answer. `人均100以下和200以上` is the second: two stated conditions collapsed into
`>= 100`, a predicate matching neither and admitting the whole band excluded twice.

Both are refused rather than raised. A `QueryPlanError` degrades to unstructured retrieval
over the raw query text, and a text search for 不是日料的旺角餐厅 matches 日料 -- so
"failing" through that path reproduces the original defect one layer down.

The approximate-price case is *not* a refusal and is asserted here as a boundary, because
the detector must not grow into it: 人均大概80 states no comparison, and the existing
contract deliberately emits no numeric constraint rather than inventing a threshold.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.ai.providers import StructuredResponse
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.retrieval.query_parser import parse_query
from tests.test_structured_retrieval import Corpus, _index


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _ask(session: Session, query: str, **kwargs: object) -> dict[str, object]:
    _index(session)
    return ConversationManager(session).ask(query, **kwargs).as_dict()  # type: ignore[arg-type]


def _parse(query: str):
    return parse_query(query, scope="personal_required", limit=10)


NEGATED = "我收藏过哪些不是日料的旺角餐厅？"
TWO_PRICES = "我收藏过哪些旺角人均100以下和200以上的日料？"


class TestNegatedConditionIsRefused:
    """P1-3. The floor: never execute the inverse of what was asked."""

    def test_negated_cuisine_never_becomes_a_positive_cuisine_constraint(self) -> None:
        plan, _ = _parse(NEGATED)
        assert plan.constraint_for("cuisine") is None

    def test_negated_cuisine_does_not_silently_broaden_to_the_district_alone(self) -> None:
        """Dropping only the cuisine would answer "旺角餐厅" -- a superset including 日料."""
        plan, diagnostics = _parse(NEGATED)
        assert plan.is_structured is False
        assert diagnostics["refused"]["code"] == "unsupported_negated_condition"

    def test_the_excluded_cuisine_is_not_presented_as_an_answer(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("旺角日料店", district="旺角", cuisine="日料", price=80)
        corpus.restaurant("旺角韩料店", district="旺角", cuisine="韩料", price=80)

        body = _ask(session, NEGATED)

        assert "旺角日料店" not in str(body["content"])

    def test_the_answer_says_which_condition_it_cannot_apply(
        self, session: Session, corpus: Corpus
    ) -> None:
        """Refusing silently is still a wrong answer: the user cannot tell it was refused."""
        corpus.restaurant("旺角日料店", district="旺角", cuisine="日料", price=80)

        body = _ask(session, NEGATED)

        assert "否定条件" in str(body["content"])

    def test_a_negated_district_is_refused_on_the_same_rule(self) -> None:
        plan, diagnostics = _parse("不是旺角的日料")
        assert plan.location is None
        assert diagnostics["refused"]["code"] == "unsupported_negated_condition"


class TestConflictingPriceConditionsAreRefused:
    """P1-4. Two stated bounds must not collapse into a third, unstated one."""

    def test_two_price_conditions_do_not_collapse_into_one_constraint(self) -> None:
        plan, diagnostics = _parse(TWO_PRICES)
        assert plan.constraint_for("price_per_person") is None
        assert diagnostics["refused"]["code"] == "conflicting_price_conditions"

    def test_the_fabricated_middle_band_is_not_returned(
        self, session: Session, corpus: Corpus
    ) -> None:
        """`>= 100` admitted the 150 restaurant, which neither stated bound allows."""
        corpus.restaurant("旺角一百五十元店", district="旺角", cuisine="日料", price=150)

        body = _ask(session, TWO_PRICES)

        assert "旺角一百五十元店" not in str(body["content"])

    def test_two_price_conditions_are_refused_with_the_marker_before_each_number(
        self,
    ) -> None:
        """人均不超过100和超过200 states both bounds as explicitly as the trailing form.

        Counting only trailing markers missed this one, and the failure was worse than the
        one it was written to catch: `_price_constraint` matched the first clause, returned
        `<= 100`, and discarded 超过200 with no diagnostic at all.
        """
        plan, diagnostics = _parse("我收藏过哪些旺角人均不超过100和超过200的日料？")
        assert plan.constraint_for("price_per_person") is None
        assert diagnostics["refused"]["code"] == "conflicting_price_conditions"

    def test_a_single_price_condition_still_parses(self) -> None:
        """The detector is a shape check, not a price-parsing rollback."""
        plan, diagnostics = _parse("我收藏过哪些旺角人均100以下的日料？")
        constraint = plan.constraint_for("price_per_person")
        assert constraint is not None
        assert (constraint.operator, constraint.value_number) == ("<", 100.0)
        assert "refused" not in diagnostics

    def test_a_single_prefix_marker_condition_still_parses(self) -> None:
        """One leading marker is one condition. Counting positions must not refuse it."""
        plan, diagnostics = _parse("我收藏过哪些旺角人均不超过100的日料？")
        constraint = plan.constraint_for("price_per_person")
        assert constraint is not None
        assert (constraint.operator, constraint.value_number) == ("<=", 100.0)
        assert "refused" not in diagnostics

    def test_one_number_marked_on_both_sides_is_still_one_condition(self) -> None:
        """人均不超过100以内 is one bound stated twice, not two conditions."""
        _, diagnostics = _parse("我收藏过哪些旺角人均不超过100以内的日料？")
        assert "refused" not in diagnostics


class TestApproximatePriceIsNotRefused:
    """Boundary. 人均大概80 stays unsupported-and-silent: no constraint, no refusal."""

    def test_approximate_price_emits_no_constraint_and_no_refusal(self) -> None:
        plan, diagnostics = _parse("我收藏过哪些旺角人均大概80的日料？")
        assert plan.constraint_for("price_per_person") is None
        assert "refused" not in diagnostics

    def test_approximate_price_still_executes_its_other_conditions(self) -> None:
        plan, _ = _parse("我收藏过哪些旺角人均大概80的日料？")
        assert plan.location is not None
        assert plan.location.district == "旺角"
        assert plan.constraint_for("cuisine") is not None


class _ProposalModel:
    """Returns a fixed proposal mixing one supported and one unsupported field."""

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    def extract(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        temperature: float = 0.0,
    ) -> StructuredResponse:
        del prompt, schema, temperature
        return StructuredResponse(data=dict(self.data), model="test")


class TestModelProposalsValidateAtomically:
    """P1-5. A proposal is one unit: an unsupported field invalidates the whole thing."""

    #: 九龙城 is a real Hong Kong district and deliberately outside the V1 vocabulary;
    #: 日本餐厅 matches no cuisine alias, so the deterministic pass finds nothing and the
    #: model is actually consulted.
    QUERY = "我收藏过哪些九龙城的日本餐厅？"

    def test_unsupported_district_reaches_validation_instead_of_being_dropped(self) -> None:
        plan, diagnostics = parse_query(
            self.QUERY,
            scope="personal_required",
            limit=10,
            structured_model=_ProposalModel({"district": "九龙城", "cuisine": "日料"}),
        )
        assert diagnostics["plan_error"]["code"] == "unknown_district"

    def test_the_surviving_field_does_not_execute_alone(self) -> None:
        """Executing the cuisine alone answers a question about a different district."""
        plan, _ = parse_query(
            self.QUERY,
            scope="personal_required",
            limit=10,
            structured_model=_ProposalModel({"district": "九龙城", "cuisine": "日料"}),
        )
        assert plan.constraint_for("cuisine") is None
        assert plan.is_structured is False

    def test_a_fully_supported_proposal_still_executes(self) -> None:
        plan, diagnostics = parse_query(
            self.QUERY,
            scope="personal_required",
            limit=10,
            structured_model=_ProposalModel({"district": "旺角", "cuisine": "日料"}),
        )
        assert "plan_error" not in diagnostics
        assert plan.location is not None
        assert plan.location.district == "旺角"
        assert plan.constraint_for("cuisine") is not None

    def test_an_entity_from_the_wrong_district_is_not_presented_as_the_answer(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("旺角日料店", district="旺角", cuisine="日料", price=80)
        _index(session)

        body = (
            ConversationManager(
                session,
                structured_model=_ProposalModel({"district": "九龙城", "cuisine": "日料"}),
            )
            .ask(self.QUERY)
            .as_dict()
        )

        assert "旺角日料店" not in str(body["content"])
