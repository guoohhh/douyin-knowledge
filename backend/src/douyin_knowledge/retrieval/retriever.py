"""Hybrid retrieval: FTS keyword + vector, fused, then resolved to evidence.

This module is the single place where the currency rule (DB-004) is enforced for
reads. Every candidate is filtered through
``SourceProcessingState.current_processing_run_id`` before it can appear in a
result. Without that filter, reprocessing a video leaves the old interpretation
retrievable and the system will happily cite a claim it no longer believes.

Only ``doc_type = 'chunk'`` documents are fused here. The indexer also writes
``doc_type = 'wiki'`` documents for wiki pages, and nothing reads them: wiki
content reaches the user through the wiki pages themselves, never through
search. That is a gap, not a design -- a wiki retrieval surface needs its own
answer for citation (a page is a synthesis, so citing it means citing the
evidence behind each statement) and is deferred to the structured retrieval
phase rather than half-added here.

Fusion uses Reciprocal Rank Fusion rather than a weighted sum of raw scores,
because BM25 and cosine similarity live on incomparable scales — BM25 is
unbounded and corpus-dependent, cosine is [-1, 1]. RRF only reads *rank*, so it
needs no per-corpus normalization constants to tune.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.extraction.grounding import is_assertable
from douyin_knowledge.knowledge.eligibility import current_eligible_runs
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.retrieval.keyword_search import KeywordSearcher
from douyin_knowledge.search.indexer import DOC_TYPE_CHUNK, DOC_TYPE_SOURCE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import EmbeddingModel
    from douyin_knowledge.retrieval.vector_store import VectorStore

logger = get_logger(__name__)

RRF_K = 60
"""Standard RRF damping constant. Larger values flatten the rank curve."""

MIN_VECTOR_SCORE = 0.25
"""Cosine floor for vector candidates.

Chosen empirically against the demo corpus: related Chinese sentences score
~0.5+, unrelated ones ~0.1. Anything at or below the noise level must not enter
fusion, because RRF reads rank and not score — one weak neighbour ranked #1 in
an otherwise empty vector list gets the same RRF weight as a strong match.
"""


@dataclass
class RetrievedChunk:
    """A retrieval unit plus the evidence it is grounded in.

    `evidence_ids` is what makes an answer citable: the chunk is a retrieval
    convenience, never a citation target, so a chunk that resolves to no
    evidence is dropped rather than shown.
    """

    chunk_id: str
    source_id: str
    text: str
    score: float
    chunk_type: str
    start_ms: int | None
    end_ms: int | None
    source_title: str | None
    evidence_ids: list[str] = field(default_factory=list)
    retrieved_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "text": self.text,
            "score": round(self.score, 6),
            "chunk_type": self.chunk_type,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source_title": self.source_title,
            "evidence_ids": self.evidence_ids,
            "retrieved_by": self.retrieved_by,
        }


@dataclass
class RetrievalResult:
    """Everything an answer generator needs, plus how it was obtained."""

    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def source_ids(self) -> list[str]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.source_id not in seen:
                seen.append(chunk.source_id)
        return seen

    def is_empty(self) -> bool:
        return not self.chunks and not self.claims


class HybridRetriever:
    """Keyword + vector retrieval with currency filtering and RRF fusion."""

    def __init__(
        self,
        session: Session,
        *,
        vector_store: VectorStore | None = None,
        embedder: EmbeddingModel | None = None,
    ) -> None:
        self.session = session
        self.keyword = KeywordSearcher(session)
        self.vector_store = vector_store
        self.embedder = embedder

    # ------------------------------------------------------------- currency

    def _current_runs(self) -> dict[str, str]:
        """Source id -> current run id, for sources policy currently allows.

        Currency answers "is this the newest successful extraction?" and policy answers
        "may this source be cited at all?"; a chunk needs both. The rule itself lives in
        `knowledge.eligibility` so retrieval, the wiki and the Knowledge API share one
        definition. Excluding here is also why exclusion needs no deletes: the rows stay on
        disk and simply stop being reachable (DEC-015).
        """
        return current_eligible_runs(self.session)

    def _current_chunk_ids(self, source_ids: Sequence[str] | None = None) -> set[str]:
        """Chunk ids belonging to each source's current run, excluding deleted sources."""
        current = self._current_runs()
        if not current:
            return set()

        stmt = select(RetrievalChunk.id, RetrievalChunk.source_id, RetrievalChunk.processing_run_id)
        stmt = stmt.join(Source, Source.id == RetrievalChunk.source_id).where(
            Source.locally_deleted_at_ms.is_(None)
        )
        if source_ids:
            stmt = stmt.where(RetrievalChunk.source_id.in_(list(source_ids)))

        return {
            chunk_id
            for chunk_id, source_id, run_id in self.session.execute(stmt)
            if current.get(source_id) == run_id
        }

    # -------------------------------------------------------------- retrieval

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        source_ids: Sequence[str] | None = None,
        use_vector: bool = True,
        include_claims: bool = True,
        candidate_multiplier: int = 4,
        min_vector_score: float = MIN_VECTOR_SCORE,
    ) -> RetrievalResult:
        """Retrieve chunks for `query`, restricted to current processing runs.

        `candidate_multiplier` over-fetches from each backend so fusion has
        enough material to reorder; returning exactly `limit` from each would
        make the fusion step nearly a no-op.

        `min_vector_score` is a relevance floor, not a tuning knob. Nearest-
        neighbour search always returns *something*: ask an unrelated question
        and it happily hands back the least-unrelated chunk in the corpus. RRF
        then ranks that noise and the answer generator cites it. A floor is what
        lets "我没找到" actually happen.
        """
        diagnostics: dict[str, Any] = {"keyword_hits": 0, "vector_hits": 0, "fts_available": False}
        allowed_chunks = self._current_chunk_ids(source_ids)
        if not allowed_chunks:
            diagnostics["reason"] = "no_current_chunks"
            return RetrievalResult(query=query, diagnostics=diagnostics)

        fetch = max(limit * candidate_multiplier, limit)
        ranked_lists: list[tuple[str, list[str]]] = []

        if self.keyword.available():
            diagnostics["fts_available"] = True
            keyword_hits = self.keyword.search(
                query,
                limit=fetch,
                doc_types=[DOC_TYPE_CHUNK],
                allowed_object_ids=sorted(allowed_chunks),
            )
            diagnostics["keyword_hits"] = len(keyword_hits)
            ranked_lists.append(("keyword", [hit.object_id for hit in keyword_hits]))

        if use_vector and self.vector_store is not None and self.embedder is not None:
            vector_hits, below_floor = self._vector_search(
                query, fetch, allowed_chunks, min_score=min_vector_score
            )
            diagnostics["vector_hits"] = len(vector_hits)
            diagnostics["vector_below_floor"] = below_floor
            ranked_lists.append(("vector", vector_hits))

        fused = self._reciprocal_rank_fusion(ranked_lists)
        if not fused:
            diagnostics["reason"] = "no_matches"
            return RetrievalResult(query=query, diagnostics=diagnostics)

        chunks = self._hydrate(fused[: limit * 2], limit=limit)
        claims = (
            self._claims_for_chunks(chunks, current=self._current_runs()) if include_claims else []
        )

        diagnostics["returned"] = len(chunks)
        diagnostics["claims"] = len(claims)
        return RetrievalResult(query=query, chunks=chunks, claims=claims, diagnostics=diagnostics)

    def _vector_search(
        self,
        query: str,
        limit: int,
        allowed_chunks: set[str],
        *,
        min_score: float = MIN_VECTOR_SCORE,
    ) -> tuple[list[str], int]:
        """Return (ids above the floor, count discarded below it)."""
        assert self.vector_store is not None and self.embedder is not None
        try:
            embedding = self.embedder.embed(query)
        except Exception as exc:  # noqa: BLE001 - degrade to keyword-only
            logger.warning("query_embedding_failed", extra={"error": str(exc)})
            return [], 0

        allowed_keys = {f"{DOC_TYPE_CHUNK}:{chunk_id}" for chunk_id in allowed_chunks}
        hits = self.vector_store.search(
            embedding.vector,
            limit=limit,
            doc_types=[DOC_TYPE_CHUNK],
            allowed_keys=allowed_keys,
        )
        kept = [hit.object_id for hit in hits if hit.score >= min_score]
        return kept, len(hits) - len(kept)

    @staticmethod
    def _reciprocal_rank_fusion(
        ranked_lists: list[tuple[str, list[str]]],
    ) -> list[tuple[str, float, list[str]]]:
        """Fuse ranked id lists by RRF: score = Σ 1/(k + rank).

        Returns (object_id, score, contributing_strategies) sorted descending.
        """
        scores: dict[str, float] = {}
        origins: dict[str, list[str]] = {}
        for strategy, ids in ranked_lists:
            for rank, object_id in enumerate(ids, start=1):
                scores[object_id] = scores.get(object_id, 0.0) + 1.0 / (RRF_K + rank)
                origins.setdefault(object_id, []).append(strategy)

        return sorted(
            ((oid, score, origins[oid]) for oid, score in scores.items()),
            key=lambda item: item[1],
            reverse=True,
        )

    def _hydrate(
        self, fused: list[tuple[str, float, list[str]]], *, limit: int
    ) -> list[RetrievedChunk]:
        """Load chunk rows and attach the evidence ids that make them citable."""
        chunk_ids = [oid for oid, _, _ in fused]
        if not chunk_ids:
            return []

        rows = self.session.execute(
            select(RetrievalChunk, Source.title)
            .join(Source, Source.id == RetrievalChunk.source_id)
            .where(RetrievalChunk.id.in_(chunk_ids))
        ).all()
        by_id = {chunk.id: (chunk, title) for chunk, title in rows}

        evidence_map: dict[str, list[str]] = {}
        for chunk_id, evidence_id in self.session.execute(
            select(RetrievalChunkEvidence.retrieval_chunk_id, RetrievalChunkEvidence.evidence_id).where(
                RetrievalChunkEvidence.retrieval_chunk_id.in_(chunk_ids)
            )
        ):
            evidence_map.setdefault(chunk_id, []).append(evidence_id)

        results: list[RetrievedChunk] = []
        for object_id, score, strategies in fused:
            entry = by_id.get(object_id)
            if entry is None:
                continue
            chunk, title = entry
            evidence_ids = evidence_map.get(chunk.id, [])
            if not evidence_ids:
                # Unciteable: a chunk with no evidence link cannot ground an
                # answer, so it is dropped rather than silently cited as the
                # chunk itself (KM-001).
                logger.debug("chunk_without_evidence", extra={"chunk_id": chunk.id})
                continue
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    source_id=chunk.source_id,
                    text=chunk.text,
                    score=score,
                    chunk_type=chunk.chunk_type,
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    source_title=title,
                    evidence_ids=evidence_ids,
                    retrieved_by=sorted(set(strategies)),
                )
            )
            if len(results) >= limit:
                break
        return results

    def _claims_for_chunks(
        self, chunks: list[RetrievedChunk], *, current: dict[str, str]
    ) -> list[Claim]:
        """Claims sharing evidence with the retrieved chunks, current runs only.

        Going through ``ClaimEvidence`` rather than matching on source keeps the
        provenance chain intact: a returned claim is guaranteed to rest on
        evidence the retrieval actually surfaced.
        """
        evidence_ids = sorted({eid for chunk in chunks for eid in chunk.evidence_ids})
        if not evidence_ids:
            return []

        claims = self.session.scalars(
            select(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .where(ClaimEvidence.evidence_id.in_(evidence_ids))
            .distinct()
        ).all()

        return [
            claim
            for claim in claims
            if current.get(claim.source_id) == claim.processing_run_id
            # An answer cites the claims it returns, so a downgraded one would arrive
            # wearing the same citation as a verified one (P1-2, DEC-017).
            and is_assertable(claim.grounding_status)
        ]

    # ------------------------------------------------------------- utilities

    def search_sources(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Title/caption search. Works at level 0, before any AI processing."""
        if not self.keyword.available():
            return []
        hits = self.keyword.search(query, limit=limit, doc_types=[DOC_TYPE_SOURCE])
        return [
            {
                "source_id": hit.object_id,
                "title": hit.title,
                "snippet": hit.snippet,
                "score": hit.score,
            }
            for hit in hits
        ]

    def evidence_by_ids(self, evidence_ids: Sequence[str]) -> dict[str, EvidenceUnit]:
        if not evidence_ids:
            return {}
        units = self.session.scalars(
            select(EvidenceUnit).where(EvidenceUnit.id.in_(list(evidence_ids)))
        ).all()
        return {unit.id: unit for unit in units}


__all__ = ["HybridRetriever", "RetrievalResult", "RetrievedChunk"]
