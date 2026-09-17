"""Deterministic Wiki maintenance; revisions and claim support remain auditable."""

from sqlalchemy import select

from .models import Claim, Entity, Source, WikiPage, WikiRevision, WikiSupport


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
        for support in supports:
            claim = session.get(Claim, support.claim_id)
            if claim is None or f"claim:{support.claim_id}" not in revision.body:
                findings.append(
                    {"page_id": page.id, "issue": "broken_support", "claim_id": support.claim_id}
                )
    return findings
