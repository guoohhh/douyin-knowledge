"""A user-state-only query answers from state, at the real entry point.

`我想去的店` is a complete question. It has no district, no cuisine and no price, and it
does not need one: `EntityUserState` is enough to select entities on its own, which is why
`QueryPlan.is_structured` counts `user_state` alongside `location`.

The defect was one layer past the executor. `StructuredExecutor` produced the matches
correctly, but a state-only plan derives no claim constraints, so those matches carry empty
`supports` -- no chunks, no claims. `RetrievalResult.is_empty()` judged emptiness on chunks
and claims alone, so `ConversationManager.ask` reported 收藏里没有匹配这个说法的内容 about
entities it had just found and qualified. An executor-level test could not see this; the
false no-result only exists on the path through `ask()`.

DEC-018 governs what a thin answer means here: UserState is user-authored state with its own
lifetime, not a claim some creator made. It does not need eligible source support behind it
to be real. So a match with no citable claim is an honest thin answer -- the entity is named,
and nothing is cited beneath it -- not an absence, and not a licence to invent a citation.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.structured import StructuredExecutor
from tests.test_structured_retrieval import Corpus, _index, _plan

STATE_QUERY = "我想去的店"


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


def _ask(session: Session, query: str = STATE_QUERY, **kwargs: object):
    _index(session)
    return ConversationManager(session).ask(query, **kwargs)  # type: ignore[arg-type]


class TestStateOnlyQueryIsStructured:
    def test_user_state_alone_makes_a_plan_structured(self) -> None:
        plan = _plan(STATE_QUERY)
        assert plan.user_state is not None
        assert plan.user_state.state == "want_to_go"
        assert plan.is_structured is True


class TestStateOnlyQueryAnswersAtTheEntryPoint:
    """P1-7. The regression the brief requires: through `ask()`, not the executor."""

    def test_ask_returns_the_entity_the_user_marked(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("我想去的旺角店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")

        result = _ask(session)

        assert "我想去的旺角店" in result.answer.content

    def test_ask_does_not_claim_the_collection_has_no_match(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("我想去的旺角店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")

        result = _ask(session)

        assert "收藏里没有匹配这个说法的内容" not in result.answer.content

    def test_an_unmarked_entity_is_not_returned(
        self, session: Session, corpus: Corpus
    ) -> None:
        corpus.restaurant("没标记的旺角店", district="旺角", cuisine="日料", price=80)
        marked = corpus.restaurant("标记过的旺角店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(marked, "want_to_go")

        result = _ask(session)

        assert "没标记的旺角店" not in result.answer.content
        assert "标记过的旺角店" in result.answer.content

    def test_a_different_state_does_not_match(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("想学的店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_learn")

        result = _ask(session)

        assert "想学的店" not in result.answer.content


class TestStateSurvivesSourceIneligibility:
    """DEC-018: the user's own intent does not expire when its source stops qualifying."""

    def test_an_excluded_source_does_not_erase_the_user_state(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("被排除来源的想去店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")
        for state in session.query(SourceProcessingState).all():
            state.current_policy_action = "exclude"
        session.flush()

        result = _ask(session)

        assert "被排除来源的想去店" in result.answer.content

    def test_no_citation_is_invented_for_a_state_only_match(
        self, session: Session, corpus: Corpus
    ) -> None:
        """A thin answer names the entity and cites nothing. It does not borrow a claim."""
        place = corpus.restaurant("被排除来源的想去店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")
        for state in session.query(SourceProcessingState).all():
            state.current_policy_action = "exclude"
        session.flush()

        result = _ask(session)

        assert result.citations == []
        assert result.answer.has_evidence is False


class TestStateOnlyResultIsNotEmpty:
    """The root cause, pinned directly: `source_ids` counted matches and `is_empty` did not."""

    def test_a_result_with_only_structured_matches_is_not_empty(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("我想去的旺角店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")
        _index(session)
        plan = _plan(STATE_QUERY)

        result = HybridRetriever(session).retrieve_structured(plan)

        assert result.structured is not None
        assert result.structured.matches != []
        assert not result.chunks and not result.claims
        assert result.is_empty() is False


class TestStateOnlyResultFeedsFollowups:
    """`第二家呢` has to resolve against a list the user was just shown."""

    def test_returned_entities_are_recorded_in_conversation_state(
        self, session: Session, corpus: Corpus
    ) -> None:
        first = corpus.restaurant("想去的第一家", district="旺角", cuisine="日料", price=80)
        second = corpus.restaurant("想去的第二家", district="旺角", cuisine="日料", price=90)
        corpus.user_state(first, "want_to_go")
        corpus.user_state(second, "want_to_go")
        _index(session)

        manager = ConversationManager(session)
        turn = manager.ask(STATE_QUERY)
        state = manager._load_state(turn.conversation_id)

        assert "想去的第一家" in state["recent_entities"]
        assert "想去的第二家" in state["recent_entities"]

    def test_a_followup_resolves_against_the_structured_list(
        self, session: Session, corpus: Corpus
    ) -> None:
        first = corpus.restaurant("想去的第一家", district="旺角", cuisine="日料", price=80)
        second = corpus.restaurant("想去的第二家", district="旺角", cuisine="日料", price=90)
        corpus.user_state(first, "want_to_go")
        corpus.user_state(second, "want_to_go")
        _index(session)

        manager = ConversationManager(session)
        turn = manager.ask(STATE_QUERY)
        followup = manager.ask("第二家呢", conversation_id=turn.conversation_id)

        assert followup.resolved_query != "第二家呢"


class TestExecutorStillMatchesState:
    def test_executor_produces_state_only_matches(
        self, session: Session, corpus: Corpus
    ) -> None:
        place = corpus.restaurant("我想去的旺角店", district="旺角", cuisine="日料", price=80)
        corpus.user_state(place, "want_to_go")
        plan = _plan(STATE_QUERY)

        outcome = StructuredExecutor(session).execute(plan)

        assert [m.entity.canonical_name for m in outcome.matches] == ["我想去的旺角店"]
