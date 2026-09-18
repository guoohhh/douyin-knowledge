"""Conversation layer: the end of the trust chain.

    Query -> scope -> retrieval -> answer -> citation

Three invariants make answers here trustworthy:

* **Scope is explicit (RET-002).** Collection knowledge and general model
  knowledge are never blended without being labelled.
* **No fake provenance.** Citations can only be minted from retrieval objects,
  and citation markers emitted by a model are validated against them.
* **Honest emptiness.** Insufficient evidence produces "我没找到" plus the
  reason, never a plausible paragraph.
"""

from douyin_knowledge.conversation.answer_generator import AnswerGenerator, GeneratedAnswer
from douyin_knowledge.conversation.citation_builder import (
    Citation,
    CitationBuilder,
    CitationSet,
)
from douyin_knowledge.conversation.conversation_manager import ConversationManager, TurnResult
from douyin_knowledge.conversation.scope import ScopeDecision, classify_scope

__all__ = [
    "AnswerGenerator",
    "Citation",
    "CitationBuilder",
    "CitationSet",
    "ConversationManager",
    "GeneratedAnswer",
    "ScopeDecision",
    "TurnResult",
    "classify_scope",
]
