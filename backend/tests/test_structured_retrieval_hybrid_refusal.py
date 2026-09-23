# ruff: noqa: E501
"""Hybrid-scope structured refusal regression.

Gap 2: For `结合我的收藏和你的知识，告诉我不是日料的旺角餐厅`, retrieval is
correctly skipped but `AnswerGenerator.generate()` took the empty-result general-knowledge
fallback before `_no_result_reasons()` could surface `diagnostics["refused"]`. The user saw
`NO_EVIDENCE_TEMPLATE` ("我没有在你已经处理的收藏里找到足够证据") even though no
collection search ran.

The fix preserves the hybrid capability but makes the distinction explicit: the collection
side says refused/unsupported rather than searched-and-empty; the general side still answers
if a general model is available. Do not claim the collection had no match when it was never
searched.

Additionally: general-only scope continues behaving as general knowledge and is not blocked
by the collection structured parser.
"""

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.answer_generator import NO_EVIDENCE_TEMPLATE, REFUSED_TEMPLATE
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.conversation.scope import SCOPE_GENERAL, SCOPE_HYBRID, SCOPE_PERSONAL_FIRST

from .test_structured_retrieval import Corpus, _index


class TestHybridRefusalDoesNotClaimSearchRan:
    """Hybrid-scope refusal states the condition was refused, not that the collection was empty."""

    @pytest.fixture
    def corpus(self, session: Session) -> Corpus:
        return Corpus(session)

    def _ask(self, session: Session, query: str, scope: str):
        _index(session)
        return ConversationManager(session).ask(query=query, scope_override=scope)

    def test_hybrid_refusal_states_the_refused_condition_not_that_the_collection_was_searched(
        self, session, corpus
    ):
        """The answer must say the condition was refused, not that the collection was empty."""
        # Negated cuisine is refused. Hybrid scope means general knowledge is allowed,
        # but the preamble must not say 我没有在你已经处理的收藏里找到足够证据 (NO_EVIDENCE_TEMPLATE),
        # which claims a search happened and found nothing. No search ran.
        result = self._ask(
            session, query="结合我的收藏和你的知识，告诉我不是日料的旺角餐厅", scope=SCOPE_HYBRID
        )
        answer = result.answer.content

        # The NO_EVIDENCE_TEMPLATE must not appear: it says a search ran and found nothing.
        assert NO_EVIDENCE_TEMPLATE not in answer

        # The REFUSED_TEMPLATE must appear instead, stating the condition was refused.
        assert REFUSED_TEMPLATE in answer

        # The refusal message itself must be present.
        assert "日料" in answer
        assert "否定条件" in answer or "不是日料" in answer

    def test_hybrid_refusal_diagnostics_include_the_refused_code_and_message(
        self, session, corpus
    ):
        """The refusal diagnostics must propagate through the result."""
        result = self._ask(
            session, query="结合我的收藏和你的知识，告诉我不是日料的旺角餐厅", scope=SCOPE_HYBRID
        )
        refused = result.answer.diagnostics.get("refused")
        assert refused is not None
        assert refused["code"] == "unsupported_negated_condition"
        assert "日料" in refused["message"]

    def test_personal_refusal_also_uses_the_refused_template_not_the_no_evidence_template(
        self, session, corpus
    ):
        """Personal scope also distinguishes refused from searched-and-empty."""
        result = self._ask(session, query="不是日料的餐厅", scope=SCOPE_PERSONAL_FIRST)
        answer = result.answer.content
        assert NO_EVIDENCE_TEMPLATE not in answer
        assert REFUSED_TEMPLATE in answer
        assert "日料" in answer


class TestGeneralOnlyScopeIsNotBlockedByStructuredParser:
    """General-only scope continues behaving as general knowledge."""

    @pytest.fixture
    def corpus(self, session: Session) -> Corpus:
        return Corpus(session)

    def _ask(self, session: Session, query: str, scope: str):
        _index(session)
        return ConversationManager(session).ask(query=query, scope_override=scope)

    def test_general_only_scope_is_not_blocked_by_a_refused_condition(self, session, corpus):
        """General scope answers directly; the collection parser does not block it."""
        result = self._ask(session, query="告诉我不是日料的旺角餐厅", scope=SCOPE_GENERAL)
        answer = result.answer.content

        # The general answer is plain; it must not mention the refusal.
        # The refusal is a collection-side concept, and general scope doesn't touch the
        # collection at all.
        assert REFUSED_TEMPLATE not in answer
        assert "否定条件" not in answer
        assert "不是日料" not in answer or "V1 无法" not in answer

        # The answer is still generated.
        assert len(answer) > 0
        assert result.answer.scope == SCOPE_GENERAL
