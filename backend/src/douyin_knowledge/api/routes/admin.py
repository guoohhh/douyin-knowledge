"""Admin API: system state, job control, index maintenance, processing policy.

Numbers here are counted from the spine, not cached. A stats page that can drift from
the database is worse than no stats page, because it is the surface a user checks when
they already suspect something is wrong.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from douyin_knowledge.api.deps import (
    AppSettings,
    DbSession,
    JobQueueDep,
    PolicyRepositoryDep,
)
from douyin_knowledge.db.models.capture import Collection, Creator, Source
from douyin_knowledge.db.models.conversation import Conversation, Message
from douyin_knowledge.db.models.entities import Claim, Entity, EntityMention
from douyin_knowledge.db.models.ops import Job, JobEvent
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun, RetrievalChunk
from douyin_knowledge.db.models.search import SearchDocument, VectorDocument
from douyin_knowledge.db.models.wiki import WikiLintFinding, WikiPage, WikiRevision
from douyin_knowledge.jobs.types import JobType, Priority
from douyin_knowledge.policy.models import PolicyAction, ProcessingRule, RuleType
from douyin_knowledge.policy.reconciler import PolicyReconciler

router = APIRouter()


def _count(db: DbSession, model: Any, *where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in where:
        stmt = stmt.where(clause)
    return db.scalar(stmt) or 0


# ----------------------------------------------------------------------- stats


@router.get("/stats")
def get_stats(db: DbSession, settings: AppSettings) -> dict[str, Any]:
    """Corpus, knowledge, index and queue counts in one call.

    `processed_sources` counts sources with a current run pointer rather than any run:
    a source whose only run failed is not processed, and counting it would overstate how
    much of the collection is actually searchable.
    """
    processing_by_status = dict(
        db.execute(
            select(
                SourceProcessingState.processing_status,
                func.count(SourceProcessingState.source_id),
            ).group_by(SourceProcessingState.processing_status)
        ).all()
    )
    jobs_by_status = dict(
        db.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        ).all()
    )
    docs_by_type = dict(
        db.execute(
            select(SearchDocument.doc_type, func.count(SearchDocument.id)).group_by(
                SearchDocument.doc_type
            )
        ).all()
    )

    return {
        "demo_mode": not settings.uses_real_providers(),
        "capture_provider": settings.capture_provider,
        "corpus": {
            "collections": _count(db, Collection),
            "creators": _count(db, Creator),
            "sources": _count(db, Source, Source.locally_deleted_at_ms.is_(None)),
            "locally_deleted": _count(db, Source, Source.locally_deleted_at_ms.is_not(None)),
            "processed_sources": _count(
                db,
                SourceProcessingState,
                SourceProcessingState.current_processing_run_id.is_not(None),
            ),
            "processing_by_status": processing_by_status,
        },
        "knowledge": {
            "evidence_units": _count(db, EvidenceUnit),
            "retrieval_chunks": _count(db, RetrievalChunk),
            "entities": _count(db, Entity, Entity.status == "active"),
            "mentions": _count(db, EntityMention),
            "ambiguous_mentions": _count(
                db, EntityMention, EntityMention.resolution_status == "ambiguous"
            ),
            "claims": _count(db, Claim),
            "wiki_pages": _count(db, WikiPage, WikiPage.status == "active"),
            "wiki_revisions": _count(db, WikiRevision),
            "open_lint_findings": _count(
                db, WikiLintFinding, WikiLintFinding.status == "open"
            ),
        },
        "index": {
            "search_documents": _count(db, SearchDocument),
            "documents_by_type": docs_by_type,
            "vector_documents": _count(db, VectorDocument),
        },
        "conversations": {
            "conversations": _count(db, Conversation),
            "messages": _count(db, Message),
        },
        "queue": {
            "by_status": jobs_by_status,
            "runs_total": _count(db, ProcessingRun),
            "runs_failed": _count(db, ProcessingRun, ProcessingRun.status == "failed"),
        },
    }


@router.get("/settings")
def get_settings_view(settings: AppSettings) -> dict[str, Any]:
    """Effective configuration with secrets reduced to booleans (SEC-002).

    The API must be able to answer "is a key configured?" without ever being able to
    answer "what is the key?", so the redaction happens in Settings rather than here.
    """
    data = settings.redacted()
    data["resolved_models"] = {
        role: settings.model_for_role(role) for role in ("extraction", "answer", "embedding", "asr")
    }
    return data


# ------------------------------------------------------------------------ jobs


@router.get("/jobs")
def list_jobs(
    db: DbSession,
    status: str | None = None,
    job_type: str | None = None,
    source_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.status == status)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    if source_id:
        stmt = stmt.where(Job.source_id == source_id)

    jobs = list(db.scalars(stmt.order_by(Job.created_at_ms.desc()).limit(limit)))
    return {
        "jobs": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "status": j.status,
                "priority": j.priority,
                "source_id": j.source_id,
                "attempt": j.attempt,
                "max_attempts": j.max_attempts,
                "available_at_ms": j.available_at_ms,
                "created_at_ms": j.created_at_ms,
                "started_at_ms": j.started_at_ms,
                "finished_at_ms": j.finished_at_ms,
                "error": j.last_error_json,
            }
            for j in jobs
        ]
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: DbSession) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    events = list(
        db.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.created_at_ms, JobEvent.id)
        )
    )
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "source_id": job.source_id,
        "payload": job.payload_json,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "created_at_ms": job.created_at_ms,
        "started_at_ms": job.started_at_ms,
        "finished_at_ms": job.finished_at_ms,
        "error": job.last_error_json,
        "events": [
            {
                "event_type": e.event_type,
                "message": e.message,
                "data": e.data_json,
                "created_at_ms": e.created_at_ms,
            }
            for e in events
        ],
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: DbSession, queue: JobQueueDep) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    cancelled = queue.cancel(job_id)
    db.refresh(job)
    return {"id": job_id, "cancelled": cancelled, "status": job.status}


class ReindexRequest(BaseModel):
    scope: Literal["fts", "vectors"] = Field(
        default="vectors", description="'vectors' also refreshes the keyword index"
    )
    source_ids: list[str] | None = None


@router.post("/reindex", status_code=202)
def reindex(request: ReindexRequest, queue: JobQueueDep) -> dict[str, Any]:
    """Queue a reindex.

    Queued rather than synchronous because a full vector rebuild calls the embedding
    provider once per document; on a real collection that is minutes of network time and
    would hold an HTTP connection open the whole way.
    """
    job_type = JobType.REBUILD_VECTORS if request.scope == "vectors" else JobType.REBUILD_FTS
    result = queue.enqueue(
        job_type,
        payload={"source_ids": request.source_ids or []},
        priority=Priority.MAINTENANCE,
    )
    return {
        "job_id": result.job.id,
        "job_type": result.job.job_type,
        "status": result.job.status,
        "created": result.created,
    }


@router.post("/wiki/maintain", status_code=202)
def queue_wiki_maintenance(queue: JobQueueDep) -> dict[str, Any]:
    result = queue.enqueue(JobType.WIKI_MAINTAIN, priority=Priority.MAINTENANCE)
    return {
        "job_id": result.job.id,
        "job_type": result.job.job_type,
        "status": result.job.status,
        "created": result.created,
    }


# -------------------------------------------------------------- processing state


@router.get("/processing/status")
def get_processing_status(
    db: DbSession, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    """Recent processing runs plus anything currently stuck or failed."""
    runs = list(
        db.scalars(
            select(ProcessingRun).order_by(ProcessingRun.started_at_ms.desc()).limit(limit)
        )
    )
    failed_states = list(
        db.scalars(
            select(SourceProcessingState)
            .where(SourceProcessingState.processing_status == "failed")
            .limit(limit)
        )
    )
    return {
        "runs": [
            {
                "id": r.id,
                "source_id": r.source_id,
                "run_kind": r.run_kind,
                "status": r.status,
                "target_level": r.target_level,
                "achieved_level": r.achieved_level,
                "started_at_ms": r.started_at_ms,
                "finished_at_ms": r.finished_at_ms,
                "models": r.models_json,
                "error": r.error_json,
            }
            for r in runs
        ],
        "failed_sources": [
            {
                "source_id": s.source_id,
                "processing_status": s.processing_status,
                "desired_level": s.desired_level,
                "achieved_level": s.achieved_level,
                "error": s.last_error_json,
                "updated_at_ms": s.updated_at_ms,
            }
            for s in failed_states
        ],
    }


# ---------------------------------------------------------------------- policy


class RuleRequest(BaseModel):
    name: str | None = None
    rule_type: Literal["source", "creator", "collection", "metadata", "semantic"]
    action: Literal["process", "metadata_only", "always_process", "exclude"]
    priority: int = 0
    is_enabled: bool = True
    target_source_id: str | None = None
    target_creator_id: str | None = None
    target_collection_id: str | None = None
    matcher: dict[str, Any] | None = None


def _rule_payload(rule: ProcessingRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "is_enabled": rule.is_enabled,
        "rule_type": str(rule.rule_type),
        "action": str(rule.action),
        "priority": rule.priority,
        "target_source_id": rule.target_source_id,
        "target_creator_id": rule.target_creator_id,
        "target_collection_id": rule.target_collection_id,
        "matcher": rule.matcher_json,
        "origin": rule.origin,
        "created_at_ms": rule.created_at_ms,
        "updated_at_ms": rule.updated_at_ms,
    }


@router.get("/policy/rules")
def list_rules(
    policy: PolicyRepositoryDep, enabled_only: bool = False
) -> dict[str, Any]:
    return {
        "rules": [_rule_payload(r) for r in policy.list_rules(enabled_only=enabled_only)]
    }


@router.post("/policy/rules", status_code=201)
def create_rule(
    request: RuleRequest, policy: PolicyRepositoryDep, db: DbSession
) -> dict[str, Any]:
    """Create a rule and immediately apply it to sources that already exist.

    Reconciling here rather than leaving it to a background job is what makes the rule feel
    like a setting instead of a request: a user who excludes a creator expects that
    creator's videos to be gone from search by the time the page re-renders (DEC-015).
    """
    rule = ProcessingRule(
        id="",  # assigned by the database default
        name=request.name,
        is_enabled=request.is_enabled,
        rule_type=RuleType(request.rule_type),
        action=PolicyAction(request.action),
        priority=request.priority,
        target_source_id=request.target_source_id,
        target_creator_id=request.target_creator_id,
        target_collection_id=request.target_collection_id,
        matcher_json=request.matcher,
        origin="user",
    )
    saved = policy.save_rule(rule)
    summary = PolicyReconciler(db, policy).reconcile_rule(saved.id)
    return {**_rule_payload(saved), "reconciled": summary.as_dict()}


@router.patch("/policy/rules/{rule_id}")
def set_rule_enabled(
    rule_id: str, enabled: bool, policy: PolicyRepositoryDep, db: DbSession
) -> dict[str, Any]:
    """Enable or disable a rule. Disabling is the reversible form of deleting it."""
    reconciler = PolicyReconciler(db, policy)
    # The affected set is read from the rule's target, which disabling does not change,
    # so order does not matter here the way it does for delete.
    updated = policy.set_rule_enabled(rule_id, enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="rule not found")
    summary = reconciler.reconcile_rule(rule_id)
    return {**_rule_payload(updated), "reconciled": summary.as_dict()}


@router.delete("/policy/rules/{rule_id}")
def delete_rule(rule_id: str, policy: PolicyRepositoryDep, db: DbSession) -> dict[str, Any]:
    reconciler = PolicyReconciler(db, policy)
    # Order is load-bearing: the affected set comes from the rule's target, and after the
    # delete there is no target to read. Computing it afterwards would fall back to a
    # whole-corpus walk, which is correct but needlessly expensive on a large library.
    affected = reconciler.affected_source_ids(rule_id)
    if not policy.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="rule not found")
    summary = reconciler.reconcile_sources(affected)
    return {"id": rule_id, "deleted": True, "reconciled": summary.as_dict()}


@router.post("/policy/reconcile")
def reconcile_policy(policy: PolicyRepositoryDep, db: DbSession) -> dict[str, Any]:
    """Re-apply every enabled rule across the corpus.

    A repair operation. Nothing should need it in normal use, but policy state is derived
    and derived state is worth being able to rebuild.
    """
    return PolicyReconciler(db, policy).reconcile_all().as_dict()


@router.get("/policy/decisions")
def list_decisions(
    policy: PolicyRepositoryDep,
    source_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    decisions = policy.list_decisions(source_id, limit=limit)
    return {
        "decisions": [
            {
                "id": d.id,
                "source_id": d.source_id,
                "rule_id": d.rule_id,
                "phase": str(d.phase),
                "action": str(d.action),
                "reason_code": d.reason_code,
                "explanation": d.explanation,
                "model_name": d.model_name,
                "created_at_ms": d.created_at_ms,
            }
            for d in decisions
        ]
    }
