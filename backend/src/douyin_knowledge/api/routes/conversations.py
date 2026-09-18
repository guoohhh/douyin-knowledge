"""Conversations API: ask questions against the collection.

One POST to `/{id}/messages` is one atomic turn (scope -> retrieve -> generate ->
persist). The route deliberately owns no logic beyond validation: `ConversationManager`
is the same entry point the CLI uses, so an answer cannot differ between surfaces.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from douyin_knowledge.api.deps import ConversationManagerDep, DbSession
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.db.models.conversation import Conversation, ConversationState

router = APIRouter()

# Spelled out rather than built from the scope constants: `Literal[]` needs literal
# values to be a valid annotation, and pydantic derives the OpenAPI enum from it.
ScopeOverride = Literal["personal_required", "personal_first", "general", "hybrid"]


class CreateConversationRequest(BaseModel):
    title: str | None = None


class AskRequest(BaseModel):
    # Unknown keys are rejected rather than ignored. Pydantic's default would silently drop
    # a misspelled `scope`, and a dropped scope override means the answer quietly crosses the
    # knowledge boundary the caller asked it to respect (RET-002) -- a wrong answer that
    # looks like a working feature. A 422 is the only honest response.
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    scope_override: ScopeOverride | None = Field(
        default=None,
        description="Force the knowledge boundary instead of inferring it from the query",
    )
    limit: int = Field(default=8, ge=1, le=30)
    source_ids: list[str] | None = Field(
        default=None, description="Restrict retrieval to these sources"
    )
    conversation_id: str | None = Field(
        default=None,
        description="Continue this conversation instead of creating a new one. "
        "Only honoured by POST /ask; the nested route takes it from the path.",
    )


@router.post("", status_code=201)
def create_conversation(
    request: CreateConversationRequest, manager: ConversationManagerDep
) -> dict[str, Any]:
    conversation = manager.create_conversation(title=request.title)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at_ms": conversation.created_at_ms,
        "updated_at_ms": conversation.updated_at_ms,
    }


@router.get("")
def list_conversations(
    manager: ConversationManagerDep, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    return {
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "created_at_ms": c.created_at_ms,
                "updated_at_ms": c.updated_at_ms,
            }
            for c in manager.list_conversations(limit=limit)
        ]
    }


@router.get("/scope-preview")
def preview_scope(q: str = Query(min_length=1)) -> dict[str, Any]:
    """Show which knowledge boundary a query would land in, before asking it.

    Exposed because the boundary is the product's core promise (RET-002): the user
    should be able to see that "我收藏里…" stays inside their own material rather than
    having to infer it from the answer.
    """
    decision = classify_scope(q)
    return {
        "scope": decision.scope,
        "matched_marker": decision.matched_marker,
        "reason": decision.reason,
        "uses_collection": decision.uses_collection,
        "allows_general_knowledge": decision.allows_general_knowledge,
        "requires_evidence": decision.requires_evidence,
    }


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str, manager: ConversationManagerDep, db: DbSession
) -> dict[str, Any]:
    conversation = manager.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    state = db.get(ConversationState, conversation_id)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at_ms": conversation.created_at_ms,
        "updated_at_ms": conversation.updated_at_ms,
        "messages": manager.list_messages(conversation_id),
        "state": state.state_json if state else {},
    }


@router.post("/{conversation_id}/messages")
def ask(
    conversation_id: str, request: AskRequest, manager: ConversationManagerDep
) -> dict[str, Any]:
    if manager.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    turn = manager.ask(
        request.query,
        conversation_id=conversation_id,
        scope_override=request.scope_override,
        limit=request.limit,
        source_ids=request.source_ids,
    )
    return turn.as_dict()


@router.post("/ask")
def ask_without_conversation(
    request: AskRequest, manager: ConversationManagerDep
) -> dict[str, Any]:
    """One-shot question. Creates the conversation implicitly, or continues a given one.

    Kept separate from the nested route so the first question of a session is a single
    round trip; a UI that had to create-then-ask would show an empty conversation if
    the second call failed. Accepting an optional `conversation_id` means a client can use
    this single endpoint for the whole thread -- follow-up resolution (DEC-004 makes
    conversation the primary interface) depends on the turn landing in the same
    conversation, and it silently did not before.
    """
    if request.conversation_id and manager.get_conversation(request.conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    turn = manager.ask(
        request.query,
        conversation_id=request.conversation_id,
        scope_override=request.scope_override,
        limit=request.limit,
        source_ids=request.source_ids,
    )
    return turn.as_dict()


@router.delete("/{conversation_id}", status_code=200)
def delete_conversation(conversation_id: str, db: DbSession) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # Messages, citations and state all cascade from the conversation row. Nothing in
    # the knowledge spine depends on a conversation, so this is a real delete rather
    # than a tombstone.
    db.delete(conversation)
    db.flush()
    return {"id": conversation_id, "deleted": True}


@router.get("/{conversation_id}/messages/{message_id}/citations")
def get_message_citations(
    conversation_id: str, message_id: str, manager: ConversationManagerDep
) -> dict[str, Any]:
    """Citations for one answer, re-read from the database rather than the response.

    This is the audit path: it proves the markers in a stored answer still resolve to
    real evidence, which is the check that would catch a wiki or retrieval regression
    after the fact.
    """
    if manager.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    citations = manager.citations.load(message_id)
    if not citations:
        # An empty list is a legitimate answer state (general-knowledge scope, or an
        # honest "no evidence"), so this is 200 with an empty list, not 404.
        return {"message_id": message_id, "citations": []}
    return {"message_id": message_id, "citations": citations}
