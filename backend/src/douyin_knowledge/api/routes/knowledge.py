"""Knowledge API: entities, wiki pages, and search.

Wiki pages are served from their *current revision* only, together with the supports
that revision recorded. A page without supports is a bug, not a display case, so the
response always carries the support list — if it comes back empty the UI can show the
page as unverified rather than quietly presenting uncited text as knowledge (WIKI-002).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from douyin_knowledge.api.deps import DbSession, RetrieverDep, WikiUpdaterDep
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import (
    Claim,
    Entity,
    EntityAlias,
    EntityMention,
)
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.db.models.wiki import (
    WikiLink,
    WikiLintFinding,
    WikiPage,
    WikiRevision,
    WikiSupport,
)

router = APIRouter()

STATUS_ACTIVE = "active"


def _current_run_ids(db: DbSession) -> dict[str, str]:
    """source_id -> current run id. Every claim read goes through this (DB-004)."""
    rows = db.execute(
        select(
            SourceProcessingState.source_id,
            SourceProcessingState.current_processing_run_id,
        ).where(SourceProcessingState.current_processing_run_id.is_not(None))
    ).all()
    return {source_id: run_id for source_id, run_id in rows}


def _live_claims(db: DbSession, stmt) -> list[Claim]:
    """Filter a claim query down to the current run of each non-deleted source."""
    current = _current_run_ids(db)
    if not current:
        return []
    deleted = set(
        db.scalars(
            select(Source.id).where(Source.locally_deleted_at_ms.is_not(None))
        ).all()
    )
    return [
        claim
        for claim in db.scalars(stmt)
        if claim.source_id not in deleted
        and current.get(claim.source_id) == claim.processing_run_id
    ]


# -------------------------------------------------------------------- entities


@router.get("/entities")
def list_entities(
    db: DbSession,
    entity_type: str | None = None,
    q: str | None = Query(default=None, description="Substring match on canonical name"),
    min_claims: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Active entities with live claim counts.

    Merged entities are excluded: they are redirects, and listing both sides of a merge
    would present one restaurant as two.
    """
    stmt = select(Entity).where(Entity.status == STATUS_ACTIVE)
    if entity_type:
        stmt = stmt.where(Entity.entity_type == entity_type)
    if q:
        stmt = stmt.where(Entity.canonical_name.ilike(f"%{q}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(stmt.order_by(Entity.canonical_name).offset(offset).limit(limit))
    )

    ids = [r.id for r in rows]
    counts: dict[str, int] = {}
    pages: dict[str, str] = {}
    if ids:
        for claim in _live_claims(
            db, select(Claim).where(Claim.subject_entity_id.in_(ids))
        ):
            if claim.subject_entity_id:
                counts[claim.subject_entity_id] = counts.get(claim.subject_entity_id, 0) + 1
        pages = {
            page.entity_id: page.id
            for page in db.scalars(
                select(WikiPage).where(
                    WikiPage.entity_id.in_(ids),
                    WikiPage.status == STATUS_ACTIVE,
                )
            )
            if page.entity_id
        }

    entities = [
        {
            "id": e.id,
            "entity_type": e.entity_type,
            "subtype": e.subtype,
            "canonical_name": e.canonical_name,
            "claim_count": counts.get(e.id, 0),
            "wiki_page_id": pages.get(e.id),
        }
        for e in rows
    ]
    if min_claims:
        entities = [e for e in entities if e["claim_count"] >= min_claims]

    return {"total": total, "limit": limit, "offset": offset, "entities": entities}


@router.get("/entities/{entity_id}")
def get_entity(entity_id: str, db: DbSession) -> dict[str, Any]:
    """One entity with its live claims, grouped by the source that said them.

    Grouping by source is not cosmetic: a claim is never a global fact (KM-003), so the
    only honest presentation is "this video said X" rather than "X is true".
    """
    entity = db.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")

    if entity.merged_into_entity_id:
        # Follow the redirect rather than rendering an empty shell: the user clicked a
        # name, and the knowledge now lives under the surviving entity.
        return {
            "id": entity.id,
            "merged_into_entity_id": entity.merged_into_entity_id,
            "status": entity.status,
            "canonical_name": entity.canonical_name,
        }

    claims = _live_claims(
        db,
        select(Claim)
        .where(
            (Claim.subject_entity_id == entity_id) | (Claim.object_entity_id == entity_id)
        )
        .order_by(Claim.created_at_ms),
    )

    source_ids = sorted({c.source_id for c in claims})
    sources: dict[str, Source] = {}
    if source_ids:
        sources = {
            s.id: s for s in db.scalars(select(Source).where(Source.id.in_(source_ids)))
        }

    aliases = list(
        db.scalars(
            select(EntityAlias.alias)
            .where(EntityAlias.entity_id == entity_id)
            .order_by(EntityAlias.alias)
        )
    )

    mention_count = (
        db.scalar(
            select(func.count())
            .select_from(EntityMention)
            .where(EntityMention.resolved_entity_id == entity_id)
        )
        or 0
    )

    page = db.scalars(
        select(WikiPage).where(
            WikiPage.entity_id == entity_id, WikiPage.status == STATUS_ACTIVE
        )
    ).first()

    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "subtype": entity.subtype,
        "canonical_name": entity.canonical_name,
        "normalized_name": entity.normalized_name,
        "status": entity.status,
        "profile": entity.profile_json,
        "aliases": aliases,
        "mention_count": mention_count,
        "wiki_page_id": page.id if page else None,
        "claims": [
            {
                "id": c.id,
                "predicate": c.predicate,
                "value_type": c.value_type,
                "value_text": c.value_text,
                "value_number": c.value_number,
                "unit": c.unit,
                "currency": c.currency,
                "claim_kind": c.claim_kind,
                "provenance_type": c.provenance_type,
                "attribution": c.attribution,
                "confidence": c.confidence,
                "source_id": c.source_id,
                "source_title": (
                    sources[c.source_id].title or sources[c.source_id].caption_raw
                    if c.source_id in sources
                    else None
                ),
                "source_url": (
                    sources[c.source_id].source_url if c.source_id in sources else None
                ),
            }
            for c in claims
        ],
    }


# ------------------------------------------------------------------------ wiki


@router.get("/wiki")
def list_wiki_pages(
    db: DbSession,
    page_type: str | None = None,
    q: str | None = None,
    status: str = Query(default=STATUS_ACTIVE),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(WikiPage).where(WikiPage.status == status)
    if page_type:
        stmt = stmt.where(WikiPage.page_type == page_type)
    if q:
        stmt = stmt.where(WikiPage.title.ilike(f"%{q}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(WikiPage.title).offset(offset).limit(limit)))

    revisions: dict[str, WikiRevision] = {}
    if rows:
        revisions = {
            rev.page_id: rev
            for rev in db.scalars(
                select(WikiRevision).where(
                    WikiRevision.page_id.in_([r.id for r in rows]),
                    WikiRevision.is_current == 1,
                )
            )
        }

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "pages": [
            {
                "id": p.id,
                "page_type": p.page_type,
                "title": p.title,
                "slug": p.slug,
                "entity_id": p.entity_id,
                "topic_id": p.topic_id,
                "status": p.status,
                "revision_no": revisions[p.id].revision_no if p.id in revisions else None,
                "summary": revisions[p.id].summary if p.id in revisions else None,
                "updated_at_ms": p.updated_at_ms,
            }
            for p in rows
        ],
    }


def _page_payload(db: DbSession, page: WikiPage) -> dict[str, Any]:
    revision = db.scalars(
        select(WikiRevision).where(
            WikiRevision.page_id == page.id, WikiRevision.is_current == 1
        )
    ).first()

    supports: list[dict[str, Any]] = []
    if revision is not None:
        rows = list(
            db.scalars(
                select(WikiSupport)
                .where(WikiSupport.wiki_revision_id == revision.id)
                .order_by(WikiSupport.statement_key, WikiSupport.id)
            )
        )
        # A support cites exactly one of claim / evidence / source (enforced by the table's
        # check constraint), so a fact-level statement has a null `source_id` and needs the
        # claim resolved to reach its source. Without this the client receives
        # `source_title: null` for every fact and a fully cited page renders as uncited --
        # which inverts the signal WIKI-002 exists to give.
        claim_sources = {
            claim.id: claim.source_id
            for claim in db.scalars(
                select(Claim).where(Claim.id.in_([r.claim_id for r in rows if r.claim_id]))
            )
        }
        evidence_sources = {
            unit.id: unit.source_id
            for unit in db.scalars(
                select(EvidenceUnit).where(
                    EvidenceUnit.id.in_([r.evidence_id for r in rows if r.evidence_id])
                )
            )
        }

        def resolved_source(row: WikiSupport) -> str | None:
            return (
                row.source_id
                or claim_sources.get(row.claim_id or "")
                or evidence_sources.get(row.evidence_id or "")
            )

        wanted = {sid for sid in (resolved_source(r) for r in rows) if sid}
        titles = {
            s.id: (s.title or s.caption_raw)
            for s in db.scalars(select(Source).where(Source.id.in_(wanted)))
        }
        supports = [
            {
                "statement_key": r.statement_key,
                "claim_id": r.claim_id,
                "evidence_id": r.evidence_id,
                # The id the support actually points at, and the source it resolves to.
                # Both are exposed: the client needs the title to display and the
                # distinction to explain what kind of citation this is.
                "source_id": r.source_id,
                "resolved_source_id": resolved_source(r),
                "source_title": titles.get(resolved_source(r) or ""),
                "cited_as": "claim" if r.claim_id else ("evidence" if r.evidence_id else "source"),
                "support_role": r.support_role,
            }
            for r in rows
        ]

    links = [
        {"to_page_id": link.to_page_id, "link_type": link.link_type}
        for link in db.scalars(select(WikiLink).where(WikiLink.from_page_id == page.id))
    ]
    findings = [
        {
            "id": f.id,
            "finding_type": f.finding_type,
            "severity": f.severity,
            "details": f.details_json,
        }
        for f in db.scalars(
            select(WikiLintFinding).where(
                WikiLintFinding.page_id == page.id, WikiLintFinding.status == "open"
            )
        )
    ]

    return {
        "id": page.id,
        "page_type": page.page_type,
        "canonical_key": page.canonical_key,
        "title": page.title,
        "slug": page.slug,
        "entity_id": page.entity_id,
        "topic_id": page.topic_id,
        "status": page.status,
        "revision": (
            {
                "id": revision.id,
                "revision_no": revision.revision_no,
                "mutation_type": revision.mutation_type,
                "content_markdown": revision.content_markdown,
                "summary": revision.summary,
                "frontmatter": revision.frontmatter_json,
                "content_hash": revision.content_hash,
                "created_at_ms": revision.created_at_ms,
            }
            if revision is not None
            else None
        ),
        "supports": supports,
        "links": links,
        "open_findings": findings,
        "updated_at_ms": page.updated_at_ms,
    }


@router.get("/wiki/{page_id}")
def get_wiki_page(page_id: str, db: DbSession) -> dict[str, Any]:
    page = db.get(WikiPage, page_id)
    if page is None:
        # Fall back to the slug so wiki URLs can be human-readable and shareable.
        page = db.scalars(select(WikiPage).where(WikiPage.slug == page_id)).first()
    if page is None:
        raise HTTPException(status_code=404, detail="wiki page not found")
    return _page_payload(db, page)


@router.get("/wiki/{page_id}/revisions")
def list_wiki_revisions(page_id: str, db: DbSession) -> dict[str, Any]:
    """Full revision history. Every mutation is auditable (WIKI-009), so the history is
    part of the product surface rather than an internal table."""
    if db.get(WikiPage, page_id) is None:
        raise HTTPException(status_code=404, detail="wiki page not found")
    revisions = list(
        db.scalars(
            select(WikiRevision)
            .where(WikiRevision.page_id == page_id)
            .order_by(WikiRevision.revision_no.desc())
        )
    )
    return {
        "page_id": page_id,
        "revisions": [
            {
                "id": r.id,
                "revision_no": r.revision_no,
                "mutation_type": r.mutation_type,
                "summary": r.summary,
                "content_hash": r.content_hash,
                "is_current": bool(r.is_current),
                "integration_run_id": r.integration_run_id,
                "created_at_ms": r.created_at_ms,
            }
            for r in revisions
        ],
    }


@router.post("/wiki/rebuild", status_code=200)
def rebuild_wiki(updater: WikiUpdaterDep) -> dict[str, Any]:
    """Recompile every page from the spine.

    Synchronous rather than queued: it is the recovery path a user reaches for when the
    wiki looks wrong, and they need to see the outcome. Composition is deterministic and
    local, so the cost is bounded by claim count, not by provider latency.
    """
    return updater.rebuild_all().as_dict()


# ---------------------------------------------------------------------- search


@router.get("/search")
def search_knowledge(
    retriever: RetrieverDep,
    q: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Hybrid retrieval, exposed raw.

    Returns the fusion diagnostics alongside the hits: when a search comes back empty
    the user deserves to know whether nothing matched or nothing has been processed yet,
    and those are very different problems (RETRIEVAL.md 15).
    """
    result = retriever.retrieve(q, limit=limit)
    return {
        "query": q,
        "results": [chunk.as_dict() for chunk in result.chunks],
        "source_ids": result.source_ids,
        "is_empty": result.is_empty(),
        "diagnostics": result.diagnostics,
    }
