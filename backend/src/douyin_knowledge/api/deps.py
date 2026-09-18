"""Shared FastAPI dependencies.

Services are built per request from the request's session. They hold a session, so
caching them across requests would hand a closed session to the next caller.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from douyin_knowledge.ai.registry import get_answer_chat_model, get_embedding_model
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.db.session import get_db
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.policy.repository import PolicyRepository
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.vector_store import VectorStore
from douyin_knowledge.wiki.updater import WikiUpdater

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_vector_store(settings: AppSettings) -> VectorStore:
    settings.ensure_directories()
    return VectorStore(settings.vector_dir)


VectorStoreDep = Annotated[VectorStore, Depends(get_vector_store)]


def get_job_queue(db: DbSession, settings: AppSettings) -> JobQueue:
    return JobQueue(db, lease_ttl_s=settings.worker_lease_ttl_s)


JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]


def get_retriever(
    db: DbSession, settings: AppSettings, store: VectorStoreDep
) -> HybridRetriever:
    return HybridRetriever(
        db, vector_store=store, embedder=get_embedding_model(settings)
    )


RetrieverDep = Annotated[HybridRetriever, Depends(get_retriever)]


def get_conversation_manager(
    db: DbSession, settings: AppSettings, store: VectorStoreDep
) -> ConversationManager:
    """Chat model is resolved eagerly, and resolves to `None` in demo mode.

    `get_answer_chat_model` -- not `get_chat_model` -- because in demo mode
    (`ai_provider=mock`) the answer must come from the deterministic composer. That is the
    point of the mode: the product answers with real cited knowledge before anyone
    configures a key, otherwise the citation path only ever gets exercised in production.
    """
    return ConversationManager(
        db,
        vector_store=store,
        embedder=get_embedding_model(settings),
        chat_model=get_answer_chat_model(settings),
        model_name=settings.model_for_role("answer"),
    )


ConversationManagerDep = Annotated[ConversationManager, Depends(get_conversation_manager)]


def get_wiki_updater(db: DbSession, settings: AppSettings) -> WikiUpdater:
    return WikiUpdater(db, model_name=settings.model_for_role("wiki_integration"))


WikiUpdaterDep = Annotated[WikiUpdater, Depends(get_wiki_updater)]


def get_policy_repository(db: DbSession) -> PolicyRepository:
    return PolicyRepository(db)


PolicyRepositoryDep = Annotated[PolicyRepository, Depends(get_policy_repository)]
