from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .capture import CapturedSource, SidecarCaptureProvider
from .db import get_session
from .models import (
    Claim,
    Entity,
    Evidence,
    Job,
    ProcessingRule,
    ProcessingRun,
    Source,
    UserState,
    WikiPage,
    WikiRevision,
    WikiSupport,
)
from .retrieval import answer, search
from .service import enqueue, ingest, reevaluate, work_once

app = FastAPI(title="Douyin Knowledge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RuleInput(BaseModel):
    dimension: str
    value: str
    action: str


class AskInput(BaseModel):
    question: str


class StateInput(BaseModel):
    state: str
    rating: int | None = None
    note: str = ""


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/dashboard")
def dashboard(session: Session = Depends(get_session)):
    return {
        "sources": session.scalar(select(func.count()).select_from(Source)),
        "ready": session.scalar(
            select(func.count()).select_from(Source).where(Source.status == "ready")
        ),
        "metadata_only": session.scalar(
            select(func.count()).select_from(Source).where(Source.status == "metadata_only")
        ),
        "queued": session.scalar(
            select(func.count()).select_from(Job).where(Job.status == "queued")
        ),
        "failed": session.scalar(
            select(func.count()).select_from(Job).where(Job.status == "failed")
        ),
    }


@app.post("/sync")
def sync(records: list[CapturedSource], session: Session = Depends(get_session)):
    return ingest(session, records)


@app.post("/sync/sidecar")
def sync_sidecar(session: Session = Depends(get_session)):
    try:
        return ingest(session, SidecarCaptureProvider().list_saves())
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/jobs/run-once")
def run_once(session: Session = Depends(get_session)):
    return {"worked": work_once(session)}


@app.get("/jobs")
def jobs(session: Session = Depends(get_session)):
    return [
        {
            "id": job.id,
            "source_id": job.source_id,
            "status": job.status,
            "attempts": job.attempts,
            "error": job.error,
        }
        for job in session.scalars(select(Job).order_by(Job.available_at.desc()).limit(100))
    ]


@app.get("/sources")
def sources(session: Session = Depends(get_session)):
    return [
        {
            "id": row.id,
            "title": row.title,
            "creator": row.creator_name,
            "collection": row.collection,
            "status": row.status,
            "policy_action": row.policy_action,
            "policy_reason": row.policy_reason,
            "url": row.url,
        }
        for row in session.scalars(select(Source).order_by(Source.created_at.desc()))
    ]


@app.get("/sources/{source_id}")
def source_detail(source_id: str, session: Session = Depends(get_session)):
    row = session.get(Source, source_id)
    if not row:
        raise HTTPException(404)
    evidence = session.scalars(select(Evidence).where(Evidence.source_id == source_id)).all()
    claims = (
        session.scalars(select(Claim).where(Claim.run_id == row.current_run_id)).all()
        if row.current_run_id
        else []
    )
    runs = session.scalars(
        select(ProcessingRun)
        .where(ProcessingRun.source_id == source_id)
        .order_by(ProcessingRun.started_at.desc())
    ).all()
    return {
        "source": {
            "id": row.id,
            "title": row.title,
            "caption": row.caption,
            "url": row.url,
            "status": row.status,
            "policy_action": row.policy_action,
            "policy_reason": row.policy_reason,
        },
        "evidence": [{"id": ev.id, "kind": ev.kind, "text": ev.text} for ev in evidence],
        "claims": [
            {"id": c.id, "text": c.value, "evidence_id": c.evidence_id, "entity_id": c.entity_id}
            for c in claims
        ],
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "provider": run.provider,
                "level": run.level,
                "error": run.error,
            }
            for run in runs
        ],
    }


@app.post("/sources/{source_id}/process")
def process(source_id: str, session: Session = Depends(get_session)):
    row = session.get(Source, source_id)
    if not row:
        raise HTTPException(404)
    # One-time processing does not remove a broad rule; explicit source override is durable.
    rule = ProcessingRule(dimension="source", value=row.external_id, action="always_process")
    session.add(rule)
    session.flush()
    reevaluate(session)
    enqueue(session, row.id, priority=100)
    session.commit()
    return {"queued": True}


@app.get("/rules")
def rules(session: Session = Depends(get_session)):
    return [
        {
            "id": r.id,
            "dimension": r.dimension,
            "value": r.value,
            "action": r.action,
            "enabled": r.enabled,
        }
        for r in session.scalars(select(ProcessingRule))
    ]


@app.post("/rules")
def add_rule(input: RuleInput, session: Session = Depends(get_session)):
    if input.dimension not in {
        "source",
        "creator",
        "collection",
        "semantic_type",
        "keyword",
    } or input.action not in {"process", "metadata_only", "always_process"}:
        raise HTTPException(422, "Invalid rule")
    rule = ProcessingRule(**input.model_dump())
    session.add(rule)
    session.commit()
    reevaluate(session)
    return {"id": rule.id}


@app.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, session: Session = Depends(get_session)):
    rule = session.get(ProcessingRule, rule_id)
    if not rule:
        raise HTTPException(404)
    session.delete(rule)
    session.commit()
    reevaluate(session)
    return {"deleted": True}


@app.get("/search")
def search_api(q: str, session: Session = Depends(get_session)):
    return search(session, q)


@app.post("/ask")
def ask(input: AskInput, session: Session = Depends(get_session)):
    return answer(session, input.question)


@app.get("/entities")
def entities(session: Session = Depends(get_session)):
    return [
        {"id": row.id, "name": row.name, "kind": row.kind}
        for row in session.scalars(select(Entity))
    ]


@app.get("/entities/{entity_id}")
def entity_detail(entity_id: str, session: Session = Depends(get_session)):
    entity = session.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    claims = session.scalars(select(Claim).where(Claim.entity_id == entity_id)).all()
    state = session.scalar(select(UserState).where(UserState.entity_id == entity_id))
    return {
        "entity": {"id": entity.id, "name": entity.name, "kind": entity.kind},
        "claims": [
            {"id": c.id, "text": c.value, "source_id": c.source_id, "evidence_id": c.evidence_id}
            for c in claims
        ],
        "user_state": {"state": state.state, "rating": state.rating, "note": state.note}
        if state
        else None,
    }


@app.put("/entities/{entity_id}/state")
def set_state(entity_id: str, input: StateInput, session: Session = Depends(get_session)):
    if not session.get(Entity, entity_id):
        raise HTTPException(404)
    state = session.scalar(select(UserState).where(UserState.entity_id == entity_id))
    if state is None:
        state = UserState(entity_id=entity_id, **input.model_dump())
        session.add(state)
    else:
        for key, value in input.model_dump().items():
            setattr(state, key, value)
    session.commit()
    return {"ok": True}


@app.get("/wiki")
def wiki(session: Session = Depends(get_session)):
    return [
        {"id": page.id, "title": page.title, "kind": page.kind, "revision": page.current_revision}
        for page in session.scalars(select(WikiPage))
    ]


@app.get("/wiki/{page_id}")
def wiki_page(page_id: str, session: Session = Depends(get_session)):
    page = session.get(WikiPage, page_id)
    if not page:
        raise HTTPException(404)
    revision = session.scalar(
        select(WikiRevision).where(
            WikiRevision.page_id == page_id, WikiRevision.number == page.current_revision
        )
    )
    supports = (
        session.scalars(select(WikiSupport).where(WikiSupport.revision_id == revision.id)).all()
        if revision
        else []
    )
    return {
        "title": page.title,
        "body": revision.body if revision else "",
        "revision": page.current_revision,
        "claim_ids": [s.claim_id for s in supports],
    }
