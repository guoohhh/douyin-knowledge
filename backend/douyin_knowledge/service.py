import logging
import os
from datetime import timedelta

from sqlalchemy import or_, select

from .ai import ExtractedClaim, Extraction, extractor
from .capture import CapturedSource
from .index import index_one
from .models import (
    Claim,
    Entity,
    EntityMention,
    Evidence,
    Job,
    KnowledgeItem,
    ProcessingRun,
    Source,
    now,
)
from .policy import evaluate
from .wiki import compile_entity
from .wiki import rebuild as rebuild_wiki

log = logging.getLogger(__name__)


def enqueue(session, source_id: str, priority: int = 0) -> Job:
    existing = session.scalar(
        select(Job).where(Job.source_id == source_id, Job.status.in_(["queued", "running"]))
    )
    if existing:
        existing.priority = max(existing.priority, priority)
        return existing
    job = Job(source_id=source_id, priority=priority)
    session.add(job)
    return job


def ingest(session, records: list[CapturedSource]) -> dict:
    created = 0
    skipped = 0
    for record in records:
        source = session.scalar(
            select(Source).where(
                Source.platform == record.platform, Source.external_id == record.external_id
            )
        )
        if source is None:
            source = Source(platform=record.platform, external_id=record.external_id)
            session.add(source)
            session.flush()
            created += 1
        for field in (
            "url",
            "title",
            "caption",
            "creator_id",
            "creator_name",
            "collection",
            "semantic_type",
            "transcript",
        ):
            setattr(source, field, getattr(record, field))
        action, reason = evaluate(session, source)
        source.policy_action, source.policy_reason = action, reason
        if action == "metadata_only":
            source.status = "metadata_only"
            skipped += 1
        elif source.current_run_id:
            source.status = "ready"
        else:
            source.status = "pending"
            enqueue(session, source.id)
        # Fixture annotations are transport data; save them only as evidence text.
        # Extraction uses them through a separate process call, never as Source facts.
        if record.claims:
            from .models import SourceAnnotation

            annotation = session.get(SourceAnnotation, source.id)
            if annotation:
                annotation.payload = record.claims
            else:
                session.add(SourceAnnotation(source_id=source.id, payload=record.claims))
    session.commit()
    rebuild_wiki(session)
    return {"created": created, "total": len(records), "metadata_only": skipped}


def reevaluate(session):
    for source in session.scalars(select(Source)):
        action, reason = evaluate(session, source)
        source.policy_action, source.policy_reason = action, reason
        if action == "metadata_only":
            source.status = "metadata_only"
            for job in session.scalars(
                select(Job).where(Job.source_id == source.id, Job.status == "queued")
            ):
                job.status = "cancelled"
        elif action != "metadata_only" and source.status == "metadata_only":
            source.status = "ready" if source.current_run_id else "pending"
            if source.status == "pending":
                enqueue(session, source.id)
    session.commit()
    rebuild_wiki(session)


def _evidence(session, source: Source) -> list[Evidence]:
    rows = []
    for kind, value in (("caption", source.caption), ("transcript", source.transcript)):
        if not value.strip():
            continue
        ev = session.scalar(
            select(Evidence).where(
                Evidence.source_id == source.id, Evidence.kind == kind, Evidence.text == value
            )
        )
        if ev is None:
            ev = Evidence(source_id=source.id, kind=kind, text=value)
            session.add(ev)
            session.flush()
        rows.append(ev)
    return rows


def _validated_claims(extraction: Extraction, evidence: list[Evidence]) -> list[ExtractedClaim]:
    lookup = {item.id: item.text for item in evidence}
    return [
        claim
        for claim in extraction.claims
        if claim.evidence_id in lookup and claim.quote in lookup[claim.evidence_id]
    ]


def process_source(session, source_id: str):
    source = session.get(Source, source_id)
    if not source:
        raise ValueError("Source missing")
    action, reason = evaluate(session, source)
    source.policy_action, source.policy_reason = action, reason
    if action == "metadata_only":
        source.status = "metadata_only"
        session.commit()
        return
    previous_entity_ids = []
    if source.current_run_id:
        previous_entity_ids = session.scalars(
            select(Claim.entity_id).where(
                Claim.run_id == source.current_run_id, Claim.entity_id.is_not(None)
            )
        ).all()
    run = ProcessingRun(
        source_id=source.id,
        provider="openai" if os.getenv("DK_OPENAI_API_KEY") else "local",
    )
    session.add(run)
    session.commit()
    try:
        evidence = _evidence(session, source)
        if not evidence:
            raise ValueError("No caption or transcript evidence; media enrichment unavailable")
        payload = [{"id": item.id, "kind": item.kind, "text": item.text} for item in evidence]
        extraction = extractor().extract(payload)
        # Demo fixture declarations are accepted only if their quote exists verbatim.
        from .models import SourceAnnotation

        annotation = session.get(SourceAnnotation, source.id)
        if annotation:
            extra = []
            for declared in annotation.payload:
                matching = next(
                    (
                        ev
                        for ev in evidence
                        if declared.get("quote", "") in ev.text and declared.get("quote")
                    ),
                    None,
                )
                if matching:
                    extra.append(
                        ExtractedClaim.model_validate({**declared, "evidence_id": matching.id})
                    )
            extraction.claims.extend(extra)
        valid = _validated_claims(extraction, evidence)
        item = KnowledgeItem(
            source_id=source.id,
            run_id=run.id,
            summary=extraction.summary,
            domain=extraction.domain,
            form=extraction.form,
            coverage="transcript" if source.transcript else "text_only",
        )
        session.add(item)
        entities = []
        for extracted in valid:
            entity = None
            if extracted.entity_name:
                normalized = " ".join(extracted.entity_name.casefold().split())
                entity = session.scalar(
                    select(Entity).where(
                        Entity.kind == extracted.entity_kind, Entity.normalized_name == normalized
                    )
                )
                if entity is None:
                    entity = Entity(
                        name=extracted.entity_name,
                        normalized_name=normalized,
                        kind=extracted.entity_kind,
                    )
                    session.add(entity)
                    session.flush()
                session.add(
                    EntityMention(
                        source_id=source.id,
                        run_id=run.id,
                        entity_id=entity.id,
                        surface=extracted.entity_name,
                        evidence_id=extracted.evidence_id,
                    )
                )
            claim = Claim(
                source_id=source.id,
                run_id=run.id,
                entity_id=entity.id if entity else None,
                evidence_id=extracted.evidence_id,
                predicate=extracted.predicate,
                value=extracted.value,
            )
            session.add(claim)
            session.flush()
            if entity:
                entities.append((entity, claim))
        source.current_run_id = run.id
        source.status = "ready"
        run.status = "succeeded"
        run.level = 2 if source.transcript else 1
        run.completed_at = now()
        for entity_id in set(previous_entity_ids + [entity.id for entity, _ in entities]):
            compile_entity(session, session.get(Entity, entity_id))
        index_one(session, source, item)
        session.commit()
    except Exception as exc:
        session.rollback()
        run = session.get(ProcessingRun, run.id)
        if run:
            run.status, run.error, run.completed_at = "failed", str(exc), now()
        source = session.get(Source, source_id)
        source.status = "ready" if source.current_run_id else "failed"
        session.commit()
        raise


def work_once(session) -> bool:
    threshold = now() - timedelta(minutes=10)
    job = session.scalar(
        select(Job)
        .where(
            or_(Job.status == "queued", (Job.status == "running") & (Job.locked_at < threshold)),
            Job.available_at <= now(),
        )
        .order_by(Job.priority.desc(), Job.available_at, Job.id)
        .limit(1)
    )
    if not job:
        return False
    job.status, job.locked_at, job.attempts = "running", now(), job.attempts + 1
    session.commit()
    try:
        process_source(session, job.source_id)
        job = session.get(Job, job.id)
        job.status, job.error = "done", ""
    except Exception as exc:
        log.exception("processing failed", extra={"source_id": job.source_id, "job_id": job.id})
        job = session.get(Job, job.id)
        job.error = str(exc)
        job.status = "failed" if job.attempts >= 3 else "queued"
        job.available_at = now() + timedelta(seconds=2**job.attempts)
    session.commit()
    return True
