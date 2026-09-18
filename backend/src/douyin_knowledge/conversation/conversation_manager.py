"""Conversation lifecycle: turns, follow-up resolution, and persistence.

``ConversationManager`` is the entry point the API calls. One ``ask`` is one
atomic turn: classify scope, resolve follow-up references, retrieve, generate,
persist the user message, the assistant message, and its citations.

Follow-up handling (RETRIEVAL.md 16) is deliberately narrow. "第二家有什么推荐菜"
is resolved by rewriting the query with entities named in the previous turn,
using ``conversation_state`` — not by feeding the whole transcript to the
retriever. Stuffing history into the query dilutes the search terms, and the
resulting drift is very hard to debug from an answer alone.

State lives in ``conversation_state``, one row per conversation, and holds only
what a follow-up needs: recently mentioned entities and the last result's source
ids. It is a cache, not a record — losing it degrades follow-ups, never history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.conversation.answer_generator import AnswerGenerator, GeneratedAnswer
from douyin_knowledge.conversation.citation_builder import CitationBuilder
from douyin_knowledge.conversation.scope import ScopeDecision, classify_scope
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.text import truncate
from douyin_knowledge.db.models.conversation import (
    Conversation,
    ConversationState,
    Message,
)
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.retrieval.retriever import HybridRetriever

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import ChatModel, EmbeddingModel
    from douyin_knowledge.retrieval.vector_store import VectorStore

logger = get_logger(__name__)

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

TITLE_LIMIT = 60
STATE_ENTITY_LIMIT = 12
STATE_SOURCE_LIMIT = 20

# Pronouns and ordinals that only make sense against the previous turn.
_FOLLOWUP_MARKERS = (
    "这家", "那家", "第一家", "第二家", "第三家", "它", "他们家", "上面那个",
    "刚才", "前面说的", "这个地方", "这几个",
)


@dataclass
class TurnResult:
    """One completed conversation turn."""

    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    answer: GeneratedAnswer
    resolved_query: str
    citations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "content": self.answer.content,
            "scope": self.answer.scope,
            "resolved_query": self.resolved_query,
            "has_evidence": self.answer.has_evidence,
            "citations": self.citations,
            "conflicts": self.answer.conflicts,
            "suggestions": self.answer.suggestions,
            "meta": self.answer.as_meta(),
        }


class ConversationManager:
    """Own the conversation turn: scope -> retrieve -> generate -> persist."""

    def __init__(
        self,
        session: Session,
        *,
        vector_store: VectorStore | None = None,
        embedder: EmbeddingModel | None = None,
        chat_model: ChatModel | None = None,
        model_name: str | None = None,
    ) -> None:
        self.session = session
        self.retriever = HybridRetriever(
            session, vector_store=vector_store, embedder=embedder
        )
        self.citations = CitationBuilder(session)
        self.generator = AnswerGenerator(chat_model=chat_model, model_name=model_name)

    # ------------------------------------------------------- conversations

    def create_conversation(self, *, title: str | None = None) -> Conversation:
        conversation = Conversation(title=title)
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.session.get(Conversation, conversation_id)

    def list_conversations(self, *, limit: int = 50) -> list[Conversation]:
        return list(
            self.session.scalars(
                select(Conversation)
                .order_by(Conversation.updated_at_ms.desc())
                .limit(limit)
            )
        )

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        messages = self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at_ms, Message.id)
        ).all()
        return [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "knowledge_scope": message.knowledge_scope,
                "created_at_ms": message.created_at_ms,
                "meta": message.response_meta_json or {},
                "citations": self.citations.load(message.id)
                if message.role == ROLE_ASSISTANT
                else [],
            }
            for message in messages
        ]

    # --------------------------------------------------------------- state

    def _load_state(self, conversation_id: str) -> dict[str, Any]:
        row = self.session.get(ConversationState, conversation_id)
        if row is None or not isinstance(row.state_json, dict):
            return {}
        return dict(row.state_json)

    def _save_state(self, conversation_id: str, state: dict[str, Any]) -> None:
        row = self.session.get(ConversationState, conversation_id)
        if row is None:
            row = ConversationState(conversation_id=conversation_id, state_json=state)
            self.session.add(row)
        else:
            row.state_json = state
            row.updated_at_ms = now_ms()

    # ----------------------------------------------------------- follow-ups

    @staticmethod
    def _is_followup(query: str) -> bool:
        return any(marker in query for marker in _FOLLOWUP_MARKERS)

    def _resolve_followup(self, query: str, state: dict[str, Any]) -> str:
        """Expand a referential query using the previous turn's entities.

        The rewrite *appends* context rather than replacing the query, so a
        misjudged follow-up degrades to a slightly broader search instead of
        answering a question the user did not ask.
        """
        if not self._is_followup(query):
            return query
        recent: list[str] = [str(n) for n in state.get("recent_entities", []) if n]
        if not recent:
            return query

        ordinal_map = {"第一家": 0, "第二家": 1, "第三家": 2}
        for marker, index in ordinal_map.items():
            if marker in query and index < len(recent):
                return f"{query} {recent[index]}"
        return f"{query} {' '.join(recent[:3])}"

    # ----------------------------------------------------------------- ask

    def ask(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        scope_override: str | None = None,
        limit: int = 8,
        source_ids: list[str] | None = None,
    ) -> TurnResult:
        """Run one full turn."""
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")

        conversation = (
            self.get_conversation(conversation_id) if conversation_id else None
        ) or self.create_conversation(title=truncate(query, TITLE_LIMIT))

        state = self._load_state(conversation.id)
        decision: ScopeDecision = classify_scope(query, override=scope_override)
        resolved_query = self._resolve_followup(query, state)

        user_message = Message(
            conversation_id=conversation.id,
            role=ROLE_USER,
            content=query,
            knowledge_scope=decision.scope,
            query_plan_json={
                "resolved_query": resolved_query,
                "scope": decision.as_dict(),
                "followup": resolved_query != query,
            },
        )
        self.session.add(user_message)
        self.session.flush()

        if decision.uses_collection:
            result = self.retriever.retrieve(
                resolved_query, limit=limit, source_ids=source_ids, include_claims=True
            )
            citation_set = self.citations.build(result)
        else:
            from douyin_knowledge.conversation.citation_builder import CitationSet
            from douyin_knowledge.retrieval.retriever import RetrievalResult

            result = RetrievalResult(query=resolved_query)
            citation_set = CitationSet()

        answer = self.generator.generate(resolved_query, result, citation_set, decision)

        assistant_message = Message(
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content=answer.content,
            knowledge_scope=answer.scope,
            query_plan_json={
                "resolved_query": resolved_query,
                "retrieval": dict(result.diagnostics),
            },
            response_meta_json=answer.as_meta(),
        )
        self.session.add(assistant_message)
        self.session.flush()

        self.citations.persist(assistant_message.id, citation_set)
        conversation.updated_at_ms = now_ms()
        if not conversation.title:
            conversation.title = truncate(query, TITLE_LIMIT)

        self._save_state(
            conversation.id,
            self._next_state(state, result=result, citation_set=citation_set),
        )

        logger.info(
            "conversation_turn",
            extra={
                "conversation_id": conversation.id,
                "scope": answer.scope,
                "citations": len(citation_set),
                "has_evidence": answer.has_evidence,
            },
        )

        return TurnResult(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            answer=answer,
            resolved_query=resolved_query,
            citations=citation_set.as_list(),
        )

    def _next_state(
        self,
        previous: dict[str, Any],
        *,
        result: Any,
        citation_set: Any,
    ) -> dict[str, Any]:
        """Carry forward just enough to resolve the next follow-up."""
        entity_names: list[str] = []
        for claim in getattr(result, "claims", []):
            subject = (claim.subject_text or "").strip()
            if subject and subject not in entity_names:
                entity_names.append(subject)

        # Keep older names behind newer ones: "第二家" usually refers to the most
        # recent list, but a two-turn-old reference should still resolve.
        for name in previous.get("recent_entities", []):
            if name not in entity_names:
                entity_names.append(str(name))

        return {
            "recent_entities": entity_names[:STATE_ENTITY_LIMIT],
            "recent_source_ids": list(getattr(result, "source_ids", []))[:STATE_SOURCE_LIMIT],
            "last_citation_count": len(citation_set),
            "updated_at_ms": now_ms(),
        }


__all__ = ["ConversationManager", "TurnResult"]
