"""Sources API: browse the captured collection and drive processing.

Everything here reads the real spine (`sources`, `evidence_units`, `processing_runs`,
`source_processing_state`). Reads that touch AI-derived data go through the currency
pointer (DB-004) so a page never mixes evidence from two different runs.

Long work is enqueued, never done in the request: a sync of a thousand saved items
cannot fit in an HTTP timeout, and the user needs to be able to close the tab.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from douyin_knowledge.api.deps import AppSettings, DbSession, JobQueueDep
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import (
    Collection,
    Creator,
    Source,
    SourceCollectionMembership,
)
from douyin_knowledge.db.models.entities import Claim, EntityMention
from douyin_knowledge.db.models.policy import (
    PolicyDecision,
    ProcessingRule,
    SourceProcessingState,
    SourceTriage,
)
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun, RetrievalChunk
from douyin_knowledge.jobs.types import JobType, Priority
from douyin_knowledge.policy.reconciler import HIDDEN_ACTIONS

router = APIRouter()


class SyncRequest(BaseModel):
    collection_external_id: str | None = Field(
        default=None,
        description="Sync one collection; omit to walk every collection the provider exposes",
    )
    auto_process: bool = Field(
        default=True, description="Enqueue processing for each synced source"
    )


class ProcessRequest(BaseModel):
    target_level: int | None = Field(default=None, ge=0, le=4)
    force: bool = Field(
        default=False,
        description="Re-run even when this source already has a current run at this level",
    )


class JobAccepted(BaseModel):
    job_id: str
    job_type: str
    status: str
    created: bool = Field(description="False when an identical job was already queued")


def _job_response(result: Any) -> JobAccepted:
    return JobAccepted(
        job_id=result.job.id,
        job_type=result.job.job_type,
        status=result.job.status,
        created=result.created,
    )


# ------------------------------------------------------------------------ sync


@router.post("/sync", response_model=JobAccepted, status_code=202)
def sync_sources(
    request: SyncRequest, queue: JobQueueDep, settings: AppSettings
) -> JobAccepted:
    """Enqueue a capture sync and return the real job id.

    The dedupe key is the *scope* of the sync, so hammering the refresh button
    returns the same job instead of queueing ten redundant provider walks.
    """
    if request.collection_external_id:
        result = queue.enqueue(
            JobType.SYNC_COLLECTION_SOURCES,
            payload={
                "external_collection_id": request.collection_external_id,
                "auto_process": request.auto_process,
            },
            dedupe_key=f"sync_collection:{request.collection_external_id}",
            priority=Priority.INTERACTIVE,
        )
    else:
        result = queue.enqueue(
            JobType.SYNC_COLLECTIONS,
            payload={"auto_process": request.auto_process},
            dedupe_key="sync_collections:all",
            priority=Priority.INTERACTIVE,
        )
    return _job_response(result)


# ---------------------------------------------------------------- collections


@router.get("/collections")
def list_collections(db: DbSession) -> dict[str, Any]:
    """Collections with their live membership counts.

    Counts only `is_present` memberships: an item the user removed from a folder
    should stop being counted there, while the membership row stays so the history of
    how they once organized things is not destroyed.
    """
    counts = dict(
        db.execute(
            select(
                SourceCollectionMembership.collection_id,
                func.count(SourceCollectionMembership.source_id),
            )
            .where(SourceCollectionMembership.is_present == 1)
            .group_by(SourceCollectionMembership.collection_id)
        ).all()
    )
    collections = db.scalars(select(Collection).order_by(Collection.name)).all()
    return {
        "collections": [
            {
                "id": c.id,
                "platform": c.platform,
                "external_collection_id": c.external_collection_id,
                "name": c.name,
                "description": c.description,
                "source_count": counts.get(c.id, 0),
                "last_synced_at_ms": c.last_synced_at_ms,
            }
            for c in collections
        ]
    }


# --------------------------------------------------------------------- listing


def _state_map(db: DbSession, source_ids: list[str]) -> dict[str, SourceProcessingState]:
    if not source_ids:
        return {}
    rows = db.scalars(
        select(SourceProcessingState).where(SourceProcessingState.source_id.in_(source_ids))
    ).all()
    return {row.source_id: row for row in rows}


def _triage_map(db: DbSession, source_ids: list[str]) -> dict[str, SourceTriage]:
    """Batched so a page of 100 sources costs one query, not 100 (DEC-016).

    Read straight from the cache table rather than through `TriageService`: a list view
    must never classify on demand. Classification belongs to the policy gate, and a GET
    that silently triggered it would make browsing cost money.
    """
    if not source_ids:
        return {}
    rows = db.scalars(select(SourceTriage).where(SourceTriage.source_id.in_(source_ids))).all()
    return {row.source_id: row for row in rows}


def _triage_block(triage: SourceTriage | None) -> dict[str, Any]:
    return {
        # `null` and `"unknown"` mean different things and the UI needs to tell them apart:
        # never classified, versus classified and the classifier could not tell (DEC-016).
        "content_type": triage.content_type if triage else None,
        "confidence": triage.confidence if triage else None,
        "method": triage.method if triage else None,
        "model_name": triage.model_name if triage else None,
        "computed_at_ms": triage.computed_at_ms if triage else None,
    }


def _source_summary(
    source: Source,
    state: SourceProcessingState | None,
    triage: SourceTriage | None = None,
) -> dict[str, Any]:
    return {
        "id": source.id,
        "platform": source.platform,
        "external_id": source.external_id,
        "source_type": source.source_type,
        "title": source.title,
        "caption": source.caption_raw,
        "creator": (
            {"id": source.creator.id, "display_name": source.creator.display_name}
            if source.creator
            else None
        ),
        "source_url": source.source_url,
        "cover_url": source.cover_url,
        "published_at_ms": source.published_at_ms,
        "saved_at_ms": source.saved_at_ms,
        "duration_ms": source.duration_ms,
        "availability": source.availability,
        "processing": {
            "status": state.processing_status if state else "pending",
            "desired_level": state.desired_level if state else None,
            "achieved_level": state.achieved_level if state else 0,
            "current_processing_run_id": state.current_processing_run_id if state else None,
            "last_success_at_ms": state.last_success_at_ms if state else None,
            # Surfaced because a processed source can still be absent from every answer:
            # the policy action, not the run pointer, decides visibility (DEC-015). A list
            # that showed only "processed" would make that look like a retrieval bug.
            "policy_action": state.current_policy_action if state else None,
            "excluded": bool(state and state.current_policy_action in HIDDEN_ACTIONS),
        },
        "triage": _triage_block(triage),
    }


@router.get("")
def list_sources(
    db: DbSession,
    source_type: str | None = None,
    creator: str | None = None,
    collection_id: str | None = None,
    processing_status: str | None = None,
    content_type: str | None = Query(
        default=None, description="Filter on the cheap triage label, e.g. `movie_clip`"
    ),
    q: str | None = Query(default=None, description="Substring match on title/caption"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List captured sources. Locally deleted items are excluded (PRIV-003)."""
    stmt = select(Source).where(Source.locally_deleted_at_ms.is_(None))

    if source_type:
        stmt = stmt.where(Source.source_type == source_type)
    if creator:
        stmt = stmt.join(Creator, Source.creator_id == Creator.id).where(
            Creator.display_name.ilike(f"%{creator}%")
        )
    if collection_id:
        stmt = stmt.join(
            SourceCollectionMembership,
            SourceCollectionMembership.source_id == Source.id,
        ).where(
            SourceCollectionMembership.collection_id == collection_id,
            SourceCollectionMembership.is_present == 1,
        )
    if processing_status:
        stmt = stmt.join(
            SourceProcessingState, SourceProcessingState.source_id == Source.id
        ).where(SourceProcessingState.processing_status == processing_status)
    if content_type:
        # Inner join on purpose: an unclassified source is not a member of any content
        # type, and returning it under a specific label would misreport what triage knows.
        stmt = stmt.join(SourceTriage, SourceTriage.source_id == Source.id).where(
            SourceTriage.content_type == content_type
        )
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(Source.title.ilike(needle) | Source.caption_raw.ilike(needle))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # `saved_at_ms` first: the collection is the user's own timeline of saving, which is
    # what they remember, and publish dates are frequently missing from the provider.
    rows = db.scalars(
        stmt.order_by(
            Source.saved_at_ms.desc().nullslast(),
            Source.first_seen_at_ms.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()

    ids = [r.id for r in rows]
    states = _state_map(db, ids)
    triages = _triage_map(db, ids)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "sources": [_source_summary(r, states.get(r.id), triages.get(r.id)) for r in rows],
    }


# ---------------------------------------------------------------------- detail


@router.get("/{source_id}")
def get_source(source_id: str, db: DbSession) -> dict[str, Any]:
    """One source with the evidence and claims from its *current* run only.

    Filtering by `current_processing_run_id` is what makes this page trustworthy: the
    database keeps every historical run, and showing all of them would present
    superseded transcriptions side by side with the live ones as if both were true.
    """
    source = db.get(Source, source_id)
    if source is None or source.locally_deleted_at_ms is not None:
        raise HTTPException(status_code=404, detail="source not found")

    state = db.get(SourceProcessingState, source_id)
    current_run_id = state.current_processing_run_id if state else None

    evidence: list[EvidenceUnit] = []
    claims: list[Claim] = []
    chunks: list[RetrievalChunk] = []
    mention_count = 0

    if current_run_id:
        chunks = list(
            db.scalars(
                select(RetrievalChunk)
                .where(RetrievalChunk.processing_run_id == current_run_id)
                .order_by(RetrievalChunk.ordinal)
            )
        )
        claims = list(
            db.scalars(
                select(Claim)
                .where(Claim.processing_run_id == current_run_id)
                .order_by(Claim.created_at_ms)
            )
        )
        mention_count = (
            db.scalar(
                select(func.count())
                .select_from(EntityMention)
                .where(EntityMention.processing_run_id == current_run_id)
            )
            or 0
        )
        # Evidence has no run column -- it is shared input, linked to runs through
        # processing_run_evidence. Ordering by time then id keeps a transcript readable.
        evidence = list(
            db.scalars(
                select(EvidenceUnit)
                .where(EvidenceUnit.source_id == source_id)
                .order_by(EvidenceUnit.start_ms.asc().nullsfirst(), EvidenceUnit.id)
            )
        )

    runs = list(
        db.scalars(
            select(ProcessingRun)
            .where(ProcessingRun.source_id == source_id)
            .order_by(ProcessingRun.started_at_ms.desc())
            .limit(10)
        )
    )

    # A hidden source has to be able to explain itself, otherwise the only honest reading
    # of this page is "processed, but mysteriously absent from every answer" (DEC-015).
    decision: PolicyDecision | None = None
    if state and state.current_policy_decision_id:
        decision = db.get(PolicyDecision, state.current_policy_decision_id)
    rule_name: str | None = None
    if decision and decision.rule_id:
        rule = db.get(ProcessingRule, decision.rule_id)
        rule_name = rule.name if rule else None

    summary = _source_summary(source, state, db.get(SourceTriage, source_id))
    summary.update(
        {
            "policy": {
                "action": state.current_policy_action if state else "process",
                "decision_id": decision.id if decision else None,
                "reason_code": decision.reason_code if decision else None,
                "phase": decision.phase if decision else None,
                "rule_id": decision.rule_id if decision else None,
                "rule_name": rule_name,
                "decided_at_ms": decision.created_at_ms if decision else None,
            },
            "collections": [
                {"id": cid, "name": name}
                for cid, name in db.execute(
                    select(Collection.id, Collection.name)
                    .join(
                        SourceCollectionMembership,
                        SourceCollectionMembership.collection_id == Collection.id,
                    )
                    .where(
                        SourceCollectionMembership.source_id == source_id,
                        SourceCollectionMembership.is_present == 1,
                    )
                ).all()
            ],
            "evidence": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "text": e.normalized_text or e.raw_text,
                    "start_ms": e.start_ms,
                    "end_ms": e.end_ms,
                    "language": e.language,
                    "confidence": e.confidence,
                }
                for e in evidence
            ],
            "chunks": [
                {
                    "id": c.id,
                    "ordinal": c.ordinal,
                    "chunk_type": c.chunk_type,
                    "text": c.text,
                    "start_ms": c.start_ms,
                    "end_ms": c.end_ms,
                }
                for c in chunks
            ],
            "claims": [
                {
                    "id": c.id,
                    "predicate": c.predicate,
                    "subject_entity_id": c.subject_entity_id,
                    "subject_text": c.subject_text,
                    "value_type": c.value_type,
                    "value_text": c.value_text,
                    "value_number": c.value_number,
                    "unit": c.unit,
                    "currency": c.currency,
                    "claim_kind": c.claim_kind,
                    "provenance_type": c.provenance_type,
                    "attribution": c.attribution,
                    "confidence": c.confidence,
                }
                for c in claims
            ],
            "mention_count": mention_count,
            "runs": [
                {
                    "id": r.id,
                    "run_kind": r.run_kind,
                    "status": r.status,
                    "target_level": r.target_level,
                    "achieved_level": r.achieved_level,
                    "is_current": r.id == current_run_id,
                    "started_at_ms": r.started_at_ms,
                    "finished_at_ms": r.finished_at_ms,
                    "models": r.models_json,
                    "error": r.error_json,
                }
                for r in runs
            ],
        }
    )
    return summary


# ------------------------------------------------------------------ processing


@router.post("/{source_id}/process", response_model=JobAccepted, status_code=202)
def process_source(
    source_id: str,
    request: ProcessRequest,
    db: DbSession,
    queue: JobQueueDep,
    settings: AppSettings,
) -> JobAccepted:
    """Enqueue processing for one source.

    `force` widens the dedupe key with a timestamp so a deliberate reprocess is not
    swallowed by the still-queued job from the automatic path.
    """
    source = db.get(Source, source_id)
    if source is None or source.locally_deleted_at_ms is not None:
        raise HTTPException(status_code=404, detail="source not found")

    target_level = request.target_level or settings.default_desired_level
    if target_level > settings.max_processing_level:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_level {target_level} exceeds max_processing_level "
                f"{settings.max_processing_level}"
            ),
        )

    job_type = JobType.REPROCESS_SOURCE if request.force else JobType.PROCESS_SOURCE
    dedupe = f"{job_type}:{source_id}:{target_level}"
    if request.force:
        dedupe = f"{dedupe}:{now_ms()}"

    result = queue.enqueue(
        job_type,
        source_id=source_id,
        payload={"target_level": target_level, "source_id": source_id},
        dedupe_key=dedupe,
        priority=Priority.INTERACTIVE,
    )
    return _job_response(result)


@router.delete("/{source_id}", status_code=200)
def delete_source_locally(source_id: str, db: DbSession) -> dict[str, Any]:
    """Local delete (PRIV-003): tombstone the source and stop serving it.

    A tombstone rather than a row delete, because the sync would otherwise re-import
    the item on the next provider walk and silently undo the user's decision.
    """
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if source.locally_deleted_at_ms is None:
        source.locally_deleted_at_ms = now_ms()
        db.flush()
    return {"id": source_id, "locally_deleted_at_ms": source.locally_deleted_at_ms}
