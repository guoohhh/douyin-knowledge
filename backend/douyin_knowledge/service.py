import hashlib
import json
import logging
import os
from datetime import timedelta

from sqlalchemy import or_, select, update

from .ai import ExtractedClaim, Extraction, extractor
from .capture import CapturedSource, CaptureProvider
from .index import index_one
from .media import OpenAITranscriber, should_transcribe
from .models import (
    Claim,
    Entity,
    EntityMention,
    Evidence,
    Job,
    KnowledgeItem,
    PolicyDecision,
    ProcessingRun,
    Source,
    SourceAnnotation,
    SourceAsset,
    SourceCollectionMembership,
    SourceSnapshot,
    SyncEvent,
    now,
)
from .policy import evaluate
from .wiki import compile_entity
from .wiki import rebuild as rebuild_wiki

log = logging.getLogger(__name__)


def checksum(payload: dict) -> str:
    value = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def record_policy(session, source: Source, action: str, reason: str):
    if (source.policy_action, source.policy_reason) == (action, reason):
        return
    rule_id = reason.split(" ", 1)[0].removeprefix("rule:") if reason.startswith("rule:") else None
    session.add(PolicyDecision(source_id=source.id, action=action, reason=reason, rule_id=rule_id))
    source.policy_action, source.policy_reason = action, reason


def cheap_semantic_type(title: str, caption: str) -> str:
    text = f"{title} {caption}".casefold()
    for label, cues in (
        ("movie_clip", ("电影片段", "电影剪辑", "movie clip")),
        ("variety_clip", ("综艺片段", "综艺剪辑", "综艺笑点")),
        ("music_clip", ("音乐片段", "歌曲剪辑", "music clip")),
    ):
        if any(cue in text for cue in cues):
            return label
    return ""


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


def ingest(session, records: list[CapturedSource], sync_kind: str = "import") -> dict:
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
        source.capture_origin = sync_kind
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
        if not source.semantic_type:
            source.semantic_type = cheap_semantic_type(source.title, source.caption)
        collection_names = list(
            dict.fromkeys(record.collections or ([record.collection] if record.collection else []))
        )
        source.collection = collection_names[0] if collection_names else ""
        if record.media_url:
            asset = session.scalar(
                select(SourceAsset).where(
                    SourceAsset.source_id == source.id, SourceAsset.kind == "video"
                )
            )
            if asset is None:
                session.add(
                    SourceAsset(source_id=source.id, kind="video", remote_url=record.media_url)
                )
            elif asset.remote_url != record.media_url:
                asset.remote_url, asset.updated_at = record.media_url, now()
        memberships = session.scalars(
            select(SourceCollectionMembership).where(
                SourceCollectionMembership.source_id == source.id
            )
        ).all()
        active = {membership.collection_name: membership for membership in memberships}
        for name, membership in active.items():
            if name not in collection_names:
                session.delete(membership)
        for name in collection_names:
            if name not in active:
                session.add(SourceCollectionMembership(source_id=source.id, collection_name=name))
        content_hash = checksum(
            {
                "title": source.title,
                "caption": source.caption,
                "transcript": source.transcript,
                "semantic_type": source.semantic_type,
            }
        )
        content_changed = source.content_checksum != content_hash
        source.content_checksum = content_hash
        snapshot_payload = {
            "platform": source.platform,
            "external_id": source.external_id,
            "url": source.url,
            "title": source.title,
            "caption": source.caption,
            "creator_id": source.creator_id,
            "creator_name": source.creator_name,
            "collections": sorted(collection_names),
            "semantic_type": source.semantic_type,
            "transcript": source.transcript,
        }
        snapshot_hash = checksum(snapshot_payload)
        existing_snapshot = session.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.source_id == source.id, SourceSnapshot.checksum == snapshot_hash
            )
        )
        if existing_snapshot is None:
            session.add(
                SourceSnapshot(
                    source_id=source.id, checksum=snapshot_hash, payload=snapshot_payload
                )
            )
        action, reason = evaluate(session, source)
        record_policy(session, source, action, reason)
        if action == "metadata_only":
            source.status = "metadata_only"
            skipped += 1
        elif (
            source.current_run_id
            and not content_changed
            and source.processed_checksum == content_hash
        ):
            source.status = "ready"
        else:
            source.status = "pending"
            enqueue(session, source.id)
        # Fixture annotations are transport data; save them only as evidence text.
        # Extraction uses them through a separate process call, never as Source facts.
        annotation = session.get(SourceAnnotation, source.id)
        if annotation:
            annotation.payload = record.claims
        elif record.claims:
            session.add(SourceAnnotation(source_id=source.id, payload=record.claims))
    if sync_kind == "sidecar":
        present = {(record.platform, record.external_id) for record in records}
        for source in session.scalars(select(Source).where(Source.capture_origin == "sidecar")):
            if (source.platform, source.external_id) in present or source.status == "removed":
                continue
            source.status = "removed"
            source.collection = ""
            for membership in session.scalars(
                select(SourceCollectionMembership).where(
                    SourceCollectionMembership.source_id == source.id
                )
            ):
                session.delete(membership)
            for job in session.scalars(
                select(Job).where(Job.source_id == source.id, Job.status == "queued")
            ):
                job.status = "cancelled"
            absent = {
                "platform": source.platform,
                "external_id": source.external_id,
                "present": False,
            }
            if not session.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.source_id == source.id,
                    SourceSnapshot.checksum == checksum(absent),
                )
            ):
                session.add(
                    SourceSnapshot(source_id=source.id, checksum=checksum(absent), payload=absent)
                )
    session.commit()
    rebuild_wiki(session)
    session.add(SyncEvent(kind=sync_kind, status="succeeded", total=len(records), created=created))
    session.commit()
    return {"created": created, "total": len(records), "metadata_only": skipped}


def sync_capture(session, provider: CaptureProvider, kind: str) -> dict:
    try:
        return ingest(session, provider.list_saves(), sync_kind=kind)
    except Exception as exc:
        session.rollback()
        reason = str(exc)[:200] if isinstance(exc, (RuntimeError, ValueError)) else ""
        session.add(SyncEvent(kind=kind, status="failed", error=f"{type(exc).__name__}: {reason}"))
        session.commit()
        raise


def reevaluate(session):
    for source in session.scalars(select(Source)):
        if source.status == "removed":
            continue
        action, reason = evaluate(session, source)
        record_policy(session, source, action, reason)
        if action == "metadata_only":
            source.status = "metadata_only"
            for job in session.scalars(
                select(Job).where(Job.source_id == source.id, Job.status == "queued")
            ):
                job.status = "cancelled"
        elif action != "metadata_only" and source.status == "metadata_only":
            source.status = (
                "ready"
                if source.current_run_id and source.processed_checksum == source.content_checksum
                else "pending"
            )
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
        if claim.evidence_id in lookup
        and claim.quote
        and claim.quote in lookup[claim.evidence_id]
        and claim.value
        and claim.value in claim.quote
    ]


def process_source(session, source_id: str):
    source = session.get(Source, source_id)
    if not source:
        raise ValueError("Source missing")
    if source.status == "removed":
        raise ValueError("Source is no longer in the sidecar collection")
    action, reason = evaluate(session, source)
    record_policy(session, source, action, reason)
    if action == "metadata_only":
        source.status = "metadata_only"
        session.commit()
        return
    captured_checksum = source.content_checksum
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
        model_name=os.getenv("DK_OPENAI_MODEL", "gpt-4.1-mini")
        if os.getenv("DK_OPENAI_API_KEY")
        else "local-rules-v1",
    )
    session.add(run)
    session.commit()
    try:
        evidence = _evidence(session, source)
        asset = session.scalar(
            select(SourceAsset).where(
                SourceAsset.source_id == source.id, SourceAsset.kind == "video"
            )
        )
        if asset and should_transcribe(source.caption, source.transcript):
            try:
                transcription = OpenAITranscriber().transcribe(asset.remote_url)
                asr = session.scalar(
                    select(Evidence).where(
                        Evidence.source_id == source.id,
                        Evidence.kind == "asr",
                        Evidence.text == transcription,
                    )
                )
                if asr is None:
                    asr = Evidence(source_id=source.id, kind="asr", text=transcription)
                    session.add(asr)
                    session.flush()
                evidence.append(asr)
            except Exception as exc:
                run.error = f"Media transcription unavailable: {exc}"
        if not evidence:
            raise ValueError(
                "No caption or transcript evidence; media enrichment unavailable"
                + (f" ({run.error})" if run.error else "")
            )
        payload = [{"id": item.id, "kind": item.kind, "text": item.text} for item in evidence]
        extraction = extractor().extract(payload)
        # Demo fixture declarations are accepted only if their quote exists verbatim.
        annotation = session.get(SourceAnnotation, source.id)
        if annotation:
            if not os.getenv("DK_OPENAI_API_KEY"):
                extraction.claims = []
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
            coverage="transcript"
            if any(ev.kind in ("transcript", "asr") for ev in evidence)
            else "text_only",
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
        session.refresh(source)
        if source.content_checksum != captured_checksum:
            raise RuntimeError("Source changed during processing; retry with current content")
        source.current_run_id = run.id
        source.processed_checksum = source.content_checksum
        source.status = "ready"
        run.status = "succeeded"
        run.level = 2 if any(ev.kind in ("transcript", "asr") for ev in evidence) else 1
        run.result_summary = {
            "evidence_count": len(evidence),
            "claim_count": len(valid),
            "entity_count": len({entity.id for entity, _ in entities}),
            "summary_excerpt": extraction.summary[:160],
            "asr_model": os.getenv("DK_OPENAI_ASR_MODEL", "whisper-1")
            if any(ev.kind == "asr" for ev in evidence)
            else "",
        }
        run.completed_at = now()
        for entity_id in set(previous_entity_ids + [entity.id for entity, _ in entities]):
            compile_entity(session, session.get(Entity, entity_id))
        index_one(session, source, item)
        session.commit()
        log.info(
            "processing_succeeded",
            extra={
                "source_id": source.id,
                "run_id": run.id,
                "provider": run.provider,
                "model_name": run.model_name,
            },
        )
    except Exception as exc:
        session.rollback()
        run = session.get(ProcessingRun, run.id)
        if run:
            run.status, run.error, run.completed_at = "failed", str(exc), now()
        source = session.get(Source, source_id)
        source.status = (
            "ready"
            if source.current_run_id and source.processed_checksum == source.content_checksum
            else "failed"
        )
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
    locked_at = now()
    eligibility = or_(
        Job.status == "queued",
        (Job.status == "running") & (Job.locked_at < threshold),
    )
    claimed = session.execute(
        update(Job)
        .where(Job.id == job.id, eligibility, Job.available_at <= locked_at)
        .values(status="running", locked_at=locked_at, attempts=Job.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    session.refresh(job)
    try:
        process_source(session, job.source_id)
        job = session.get(Job, job.id)
        job.status, job.error = "done", ""
    except Exception as exc:
        log.error(
            "processing_failed",
            extra={
                "source_id": job.source_id,
                "job_id": job.id,
                "error_type": type(exc).__name__,
            },
        )
        job = session.get(Job, job.id)
        job.error = str(exc)
        job.status = "failed" if job.attempts >= 3 else "queued"
        job.available_at = now() + timedelta(seconds=2**job.attempts)
    session.commit()
    return True
