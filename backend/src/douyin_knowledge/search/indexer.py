"""Builds the rebuildable search projections: ``search_documents`` + ``vector_documents``.

Both tables are *derived state* (DATA_SCHEMA part H). Nothing here is canonical
truth; the whole projection can be dropped and rebuilt from the provenance spine
at any time, and `reindex_all` does exactly that.

Two rules shape this module:

1. **Currency (DB-004).** Only evidence belonging to a source's
   ``current_processing_run_id`` is indexed. Reprocessing a source creates a new
   run; its old chunks must stop being retrievable the moment the pointer moves,
   otherwise search answers from a superseded interpretation of the video.
2. **Hash-guarded writes.** A document is rewritten only when its content hash
   changes. Rewriting unconditionally would fire the FTS triggers on every row
   of every reindex, which is both slow and pointless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.text import content_hash, normalize_ws
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Entity, EntityAlias
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import RetrievalChunk
from douyin_knowledge.db.models.search import SearchDocument, VectorDocument
from douyin_knowledge.db.models.wiki import WikiPage, WikiRevision
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.search.tokenizer import TOKENIZER_VERSION, segment_for_index

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import EmbeddingModel
    from douyin_knowledge.retrieval.vector_store import VectorStore

logger = get_logger(__name__)

DOC_TYPE_CHUNK = "chunk"
DOC_TYPE_SOURCE = "source"
DOC_TYPE_ENTITY = "entity"
DOC_TYPE_WIKI = "wiki_page"


@dataclass(frozen=True)
class IndexCandidate:
    """A single unit of indexable text, resolved from the provenance spine."""

    doc_type: str
    object_id: str
    title: str | None
    body: str
    metadata: dict[str, object]

    @property
    def content_hash(self) -> str:
        # TOKENIZER_VERSION participates so a segmentation change invalidates rows.
        return content_hash(
            TOKENIZER_VERSION, self.doc_type, self.object_id, self.title or "", self.body
        )


@dataclass
class IndexStats:
    """What a reindex actually did — surfaced by the admin API and the CLI."""

    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    embedded: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "embedded": self.embedded,
        }


def _current_run_ids(session: Session, source_ids: Sequence[str] | None = None) -> dict[str, str]:
    """Map source_id -> current_processing_run_id, skipping sources with no run.

    This is the currency filter from DB-004 in its most reusable form.
    """
    stmt = select(
        SourceProcessingState.source_id, SourceProcessingState.current_processing_run_id
    ).where(SourceProcessingState.current_processing_run_id.is_not(None))
    if source_ids:
        stmt = stmt.where(SourceProcessingState.source_id.in_(list(source_ids)))
    return {row[0]: row[1] for row in session.execute(stmt) if row[1]}


def collect_chunk_candidates(
    session: Session, source_ids: Sequence[str] | None = None
) -> list[IndexCandidate]:
    """Retrieval chunks from the *current* run of each source."""
    current = _current_run_ids(session, source_ids)
    if not current:
        return []

    stmt = select(RetrievalChunk, Source).join(Source, Source.id == RetrievalChunk.source_id)
    if source_ids:
        stmt = stmt.where(RetrievalChunk.source_id.in_(list(source_ids)))

    candidates: list[IndexCandidate] = []
    for chunk, source in session.execute(stmt).all():
        if current.get(chunk.source_id) != chunk.processing_run_id:
            continue  # superseded by a newer run
        if source.locally_deleted_at_ms is not None:
            continue
        body = normalize_ws(chunk.text or "")
        if not body:
            continue
        candidates.append(
            IndexCandidate(
                doc_type=DOC_TYPE_CHUNK,
                object_id=chunk.id,
                title=source.title,
                body=segment_for_index(body),
                metadata={
                    "source_id": chunk.source_id,
                    "processing_run_id": chunk.processing_run_id,
                    "chunk_type": chunk.chunk_type,
                    "start_ms": chunk.start_ms,
                    "end_ms": chunk.end_ms,
                    "raw_text": body,
                },
            )
        )
    return candidates


def collect_source_candidates(
    session: Session, source_ids: Sequence[str] | None = None
) -> list[IndexCandidate]:
    """Source-level metadata (title + caption).

    Indexed independently of processing level so a source is findable by title
    the moment it is captured, before any AI has looked at it.
    """
    stmt = select(Source).where(Source.locally_deleted_at_ms.is_(None))
    if source_ids:
        stmt = stmt.where(Source.id.in_(list(source_ids)))

    candidates: list[IndexCandidate] = []
    for source in session.scalars(stmt):
        parts = [source.title or "", source.caption_raw or ""]
        body = normalize_ws(" ".join(p for p in parts if p))
        if not body:
            continue
        candidates.append(
            IndexCandidate(
                doc_type=DOC_TYPE_SOURCE,
                object_id=source.id,
                title=source.title,
                body=segment_for_index(body),
                metadata={
                    "source_id": source.id,
                    "platform": source.platform,
                    "raw_text": body,
                },
            )
        )
    return candidates


def collect_entity_candidates(session: Session) -> list[IndexCandidate]:
    """Entities with their aliases, so 好运 finds 好运茶餐厅."""
    # Aliases are fetched in one pass rather than per-entity; there is no ORM
    # relationship declared between Entity and EntityAlias.
    alias_map: dict[str, list[str]] = {}
    for entity_id, alias in session.execute(select(EntityAlias.entity_id, EntityAlias.alias)):
        alias_map.setdefault(entity_id, []).append(alias)

    candidates: list[IndexCandidate] = []
    stmt = select(Entity).where(Entity.merged_into_entity_id.is_(None))
    for entity in session.scalars(stmt):
        body = normalize_ws(" ".join([entity.canonical_name, *alias_map.get(entity.id, [])]))
        candidates.append(
            IndexCandidate(
                doc_type=DOC_TYPE_ENTITY,
                object_id=entity.id,
                title=entity.canonical_name,
                body=segment_for_index(body),
                metadata={
                    "entity_id": entity.id,
                    "entity_type": entity.entity_type,
                    "raw_text": body,
                },
            )
        )
    return candidates


def collect_wiki_candidates(session: Session) -> list[IndexCandidate]:
    """Current wiki revisions only — history is not searchable."""
    stmt = (
        select(WikiPage, WikiRevision)
        .join(WikiRevision, WikiRevision.page_id == WikiPage.id)
        .where(WikiRevision.is_current.is_(True))
        .where(WikiPage.status != "archived")
    )
    candidates: list[IndexCandidate] = []
    for page, revision in session.execute(stmt).all():
        body = normalize_ws(revision.content_markdown or "")
        if not body:
            continue
        candidates.append(
            IndexCandidate(
                doc_type=DOC_TYPE_WIKI,
                object_id=page.id,
                title=page.title,
                body=segment_for_index(body),
                metadata={
                    "wiki_page_id": page.id,
                    "page_type": page.page_type,
                    "revision_id": revision.id,
                    "raw_text": body,
                },
            )
        )
    return candidates


def sync_documents(
    session: Session,
    candidates: Iterable[IndexCandidate],
    *,
    doc_types: Sequence[str],
    prune: bool = True,
    stats: IndexStats | None = None,
) -> IndexStats:
    """Reconcile ``search_documents`` for the given doc types against `candidates`.

    `prune` deletes rows whose object no longer produces a candidate — that is
    what removes a chunk from search after reprocessing supersedes its run. It
    must stay off for incremental single-source indexing, where `candidates`
    covers only a slice of the corpus and everything else would look orphaned.
    """
    stats = stats or IndexStats()
    candidate_list = list(candidates)

    existing = {
        (doc.doc_type, doc.object_id): doc
        for doc in session.scalars(
            select(SearchDocument).where(SearchDocument.doc_type.in_(list(doc_types)))
        )
    }

    seen: set[tuple[str, str]] = set()
    for candidate in candidate_list:
        stats.scanned += 1
        key = (candidate.doc_type, candidate.object_id)
        seen.add(key)
        current = existing.get(key)
        digest = candidate.content_hash

        if current is None:
            session.add(
                SearchDocument(
                    doc_type=candidate.doc_type,
                    object_id=candidate.object_id,
                    title=candidate.title,
                    body=candidate.body,
                    metadata_json=candidate.metadata,
                    content_hash=digest,
                )
            )
            stats.inserted += 1
        elif current.content_hash != digest:
            current.title = candidate.title
            current.body = candidate.body
            current.metadata_json = candidate.metadata
            current.content_hash = digest
            current.updated_at_ms = now_ms()
            stats.updated += 1
        else:
            stats.unchanged += 1

    if prune:
        for key, doc in existing.items():
            if key not in seen:
                session.delete(doc)
                stats.deleted += 1

    return stats


def sync_vectors(
    session: Session,
    store: VectorStore,
    embedder: EmbeddingModel,
    *,
    doc_types: Sequence[str] = (DOC_TYPE_CHUNK,),
    model_name: str = "unknown",
    stats: IndexStats | None = None,
) -> IndexStats:
    """Embed any search document whose vector is missing or stale.

    Reads from ``search_documents`` rather than re-deriving from the spine, so
    the keyword and vector indexes are guaranteed to describe the same corpus.
    Embedding uses the un-segmented ``raw_text`` from metadata: jieba spacing is
    an FTS artifact and would only add noise to an embedding.
    """
    stats = stats or IndexStats()

    docs = list(
        session.scalars(select(SearchDocument).where(SearchDocument.doc_type.in_(list(doc_types))))
    )
    tracked = {
        (v.doc_type, v.object_id): v
        for v in session.scalars(
            select(VectorDocument).where(
                VectorDocument.doc_type.in_(list(doc_types)),
                VectorDocument.embedding_model == model_name,
            )
        )
    }

    pending: list[tuple[SearchDocument, str]] = []
    for doc in docs:
        stats.scanned += 1
        existing = tracked.get((doc.doc_type, doc.object_id))
        if existing is not None and existing.content_hash == doc.content_hash:
            stats.unchanged += 1
            continue
        meta = doc.metadata_json or {}
        text = str(meta.get("raw_text") or doc.body)
        pending.append((doc, text))

    live_keys = {f"{d.doc_type}:{d.object_id}" for d in docs}

    if pending:
        embeddings = embedder.embed_batch([text for _, text in pending])
        for (doc, _), embedding in zip(pending, embeddings, strict=True):
            vector_key = f"{doc.doc_type}:{doc.object_id}"
            store.upsert(
                vector_key,
                embedding.vector,
                metadata={"doc_type": doc.doc_type, "object_id": doc.object_id},
            )
            existing = tracked.get((doc.doc_type, doc.object_id))
            if existing is None:
                session.add(
                    VectorDocument(
                        doc_type=doc.doc_type,
                        object_id=doc.object_id,
                        content_hash=doc.content_hash,
                        embedding_model=embedding.model or model_name,
                        embedding_dim=embedding.dimensions,
                        vector_key=vector_key,
                    )
                )
                stats.inserted += 1
            else:
                existing.content_hash = doc.content_hash
                existing.embedding_dim = embedding.dimensions
                existing.vector_key = vector_key
                existing.indexed_at_ms = now_ms()
                stats.updated += 1
            stats.embedded += 1

    # Drop vectors whose search document disappeared, then persist to disk.
    stats.deleted += store.prune(live_keys)
    for vec_doc in tracked.values():
        if f"{vec_doc.doc_type}:{vec_doc.object_id}" not in live_keys:
            session.delete(vec_doc)
    store.save()
    return stats


def reindex_source(session: Session, source_id: str) -> IndexStats:
    """Incremental keyword reindex for one source after processing.

    Chunk pruning is scoped by hand here: `sync_documents(prune=True)` would
    delete every other source's chunks, since they are absent from this slice.
    """
    stats = IndexStats()
    sync_documents(
        session,
        collect_source_candidates(session, [source_id]),
        doc_types=[DOC_TYPE_SOURCE],
        prune=False,
        stats=stats,
    )

    chunk_candidates = collect_chunk_candidates(session, [source_id])
    keep = {c.object_id for c in chunk_candidates}
    for doc in session.scalars(
        select(SearchDocument).where(SearchDocument.doc_type == DOC_TYPE_CHUNK)
    ).all():
        meta = doc.metadata_json or {}
        if meta.get("source_id") == source_id and doc.object_id not in keep:
            session.delete(doc)
            stats.deleted += 1

    sync_documents(session, chunk_candidates, doc_types=[DOC_TYPE_CHUNK], prune=False, stats=stats)
    return stats


def reindex_all(
    session: Session,
    *,
    store: VectorStore | None = None,
    embedder: EmbeddingModel | None = None,
    model_name: str = "unknown",
) -> IndexStats:
    """Full rebuild of every projection. Safe to run at any time by design."""
    stats = IndexStats()
    sync_documents(
        session, collect_source_candidates(session), doc_types=[DOC_TYPE_SOURCE], stats=stats
    )
    sync_documents(
        session, collect_chunk_candidates(session), doc_types=[DOC_TYPE_CHUNK], stats=stats
    )
    sync_documents(
        session, collect_entity_candidates(session), doc_types=[DOC_TYPE_ENTITY], stats=stats
    )
    sync_documents(session, collect_wiki_candidates(session), doc_types=[DOC_TYPE_WIKI], stats=stats)
    session.flush()

    if store is not None and embedder is not None:
        sync_vectors(
            session,
            store,
            embedder,
            doc_types=(DOC_TYPE_CHUNK, DOC_TYPE_SOURCE),
            model_name=model_name,
            stats=stats,
        )

    logger.info("search_reindex_complete", extra={"stats": stats.as_dict()})
    return stats


def clear_index(session: Session) -> int:
    """Drop the whole projection. The FTS triggers cascade the deletion."""
    result = session.execute(delete(SearchDocument))
    session.execute(delete(VectorDocument))
    return int(result.rowcount or 0)
