"""Deterministic Wiki maintenance; revisions and claim support remain auditable."""

import re

from sqlalchemy import select

from .models import (
    Claim,
    Entity,
    Source,
    WikiPage,
    WikiQualityEvent,
    WikiRevision,
    WikiSupport,
    now,
)


def compile_entity(session, entity: Entity) -> bool:
    page = session.scalar(
        select(WikiPage).where(WikiPage.kind == "entity", WikiPage.key == entity.id)
    )
    claims = session.scalars(
        select(Claim)
        .join(Source, Claim.source_id == Source.id)
        .where(
            Claim.entity_id == entity.id,
            Claim.run_id == Source.current_run_id,
            Source.status == "ready",
        )
        .order_by(Claim.id)
    ).all()
    if page is None and not claims:
        return False
    if page is None:
        page = WikiPage(kind="entity", key=entity.id, title=entity.name)
        session.add(page)
        session.flush()
    lines = [f"# {entity.name}", "", "## 来源观点"]
    for claim in claims:
        source = session.get(Source, claim.source_id)
        lines.append(
            f"- {claim.value} [来源: {source.title or source.external_id}; claim:{claim.id}]"
        )
    if not claims:
        lines.append("当前没有符合处理策略的来源观点。")
    body = "\n".join(lines)
    previous = (
        session.scalar(
            select(WikiRevision).where(
                WikiRevision.page_id == page.id, WikiRevision.number == page.current_revision
            )
        )
        if page.current_revision
        else None
    )
    if previous and previous.body == body:
        supported = set(
            session.scalars(
                select(WikiSupport.claim_id).where(WikiSupport.revision_id == previous.id)
            ).all()
        )
        if supported == {claim.id for claim in claims}:
            return False
    page.current_revision += 1
    revision = WikiRevision(page_id=page.id, number=page.current_revision, body=body)
    session.add(revision)
    session.flush()
    for claim in claims:
        session.add(WikiSupport(revision_id=revision.id, claim_id=claim.id))
    return True


def rebuild(session) -> int:
    count = sum(compile_entity(session, entity) for entity in session.scalars(select(Entity)))
    session.commit()
    return count


def lint(session) -> list[dict]:
    findings = []
    for page in session.scalars(select(WikiPage)):
        revision = session.scalar(
            select(WikiRevision).where(
                WikiRevision.page_id == page.id,
                WikiRevision.number == page.current_revision,
            )
        )
        if revision is None:
            findings.append({"page_id": page.id, "issue": "missing_revision"})
            continue
        supports = session.scalars(
            select(WikiSupport).where(WikiSupport.revision_id == revision.id)
        ).all()
        mentioned = set(re.findall(r"claim:([a-f0-9]{32})", revision.body))
        supported = {support.claim_id for support in supports}
        for claim_id in mentioned - supported:
            findings.append({"page_id": page.id, "issue": "missing_support", "claim_id": claim_id})
        for support in supports:
            claim = session.get(Claim, support.claim_id)
            if claim is None or f"claim:{support.claim_id}" not in revision.body:
                findings.append(
                    {"page_id": page.id, "issue": "broken_support", "claim_id": support.claim_id}
                )
            elif (
                source := session.get(Source, claim.source_id)
            ).status != "ready" or source.current_run_id != claim.run_id:
                findings.append(
                    {"page_id": page.id, "issue": "stale_support", "claim_id": support.claim_id}
                )
    return findings


def audit(session) -> list[dict]:
    findings = lint(session)
    active = {
        (finding["page_id"], finding["issue"], finding.get("claim_id", "")) for finding in findings
    }
    open_events = session.scalars(
        select(WikiQualityEvent).where(WikiQualityEvent.status == "open")
    ).all()
    existing = {(event.page_id, event.issue, event.detail): event for event in open_events}
    for page_id, issue, detail in active - existing.keys():
        session.add(WikiQualityEvent(page_id=page_id, issue=issue, detail=detail))
    for key, event in existing.items():
        if key not in active:
            event.status, event.resolved_at = "resolved", now()
    session.commit()
    return findings


def fix(session) -> dict:
    before = audit(session)
    revised = rebuild(session)
    remaining = audit(session)
    return {"found": len(before), "revised_pages": revised, "remaining": remaining}
