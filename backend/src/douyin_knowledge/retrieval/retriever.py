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
from douyin_knowledge.knowledge.eligibility import (
    apply_claim_eligibility,
    current_eligible_runs,
)
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.retrieval.keyword_search import KeywordSearcher
from douyin_knowledge.retrieval.structured import (
    StructuredExecutor,
    StructuredMatch,
    StructuredResult,
)
from douyin_knowledge.search.indexer import DOC_TYPE_CHUNK, DOC_TYPE_SOURCE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import EmbeddingModel
    from douyin_knowledge.retrieval.query_plan import QueryPlan
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
    structured: StructuredResult | None = None

    @property
    def source_ids(self) -> list[str]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.source_id not in seen:
                seen.append(chunk.source_id)
        # Structured matches contribute their sources even when no chunk was retrieved: a
        # qualifying entity whose supporting video produced no retrievable chunk still has
        # real provenance. Without this, such a result would report "0 相关来源" while
        # displaying cited claims.
        if self.structured is not None:
            for source_id in self.structured.qualifying_source_ids:
                if source_id not in seen:
                    seen.append(source_id)
        return seen

    def is_empty(self) -> bool:
        """Whether there is nothing to answer from.

        Structured matches count, for the same reason they count in `source_ids` above.
        A user-state-only plan (我想去的店) qualifies entities out of `EntityUserState`
        and derives no claim constraints, so it produces matches with empty `supports`
        and therefore no chunks and no claims. Judging emptiness on chunks and claims
        alone called that a no-result and told the user 收藏里没有匹配这个说法的内容 --
        false, because the entity was found and did qualify. UserState is user-authored
        state with its own lifetime (DEC-018); it does not need a creator claim behind
        it to be real, so a match without claims is a thin answer, not an absent one.
        """
        return not self.chunks and not self.claims and not self._has_structured_matches()

    def _has_structured_matches(self) -> bool:
        return self.structured is not None and bool(self.structured.matches)


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
        if source_ids is not None:
            # `is not None`, not truthiness: an empty allow-list means "restricted to no
            # sources" and must return nothing. Treating it as "no restriction" would turn
            # the narrowest possible request into an unrestricted search over the corpus.
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
        claims = self._claims_for_chunks(chunks) if include_claims else []

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

    def _claims_for_chunks(self, chunks: list[RetrievedChunk]) -> list[Claim]:
        """Claims sharing evidence with the retrieved chunks, via shared eligibility.

        Two conditions, and they are different questions. *Relevance* is the evidence
        overlap: a returned claim rests on evidence this retrieval actually surfaced, which
        is why the join goes through ``ClaimEvidence`` rather than matching on source.
        *Eligibility* is *whether the claim may be presented at all*, and that is not this
        function's question to answer -- it belongs to ``knowledge/eligibility.py``, which
        is the one definition of current normal knowledge.

        This used to hand-check two of the six rules (run currency and assertability) and
        was a leak for exactly the reason that module's docstring gives: a surface applying
        a subset of the rules is a subset of the guarantee. The missing rule that mattered
        was 6 -- a claim needs evidence from its *own* source. Without it, the corrupted
        shape ``Claim(source=A) -> ClaimEvidence -> Evidence(source=B)`` was selectable
        here whenever B's chunk was retrieved: the claim rode in on another source's
        evidence, which is the fabricated provenance the structured path had already been
        fixed to refuse. Rules 1 and 3 were missing too, so a locally-deleted or
        policy-hidden source's claims could arrive through this path.
        """
        evidence_ids = sorted({eid for chunk in chunks for eid in chunk.evidence_ids})
        if not evidence_ids:
            return []

        stmt = apply_claim_eligibility(
            select(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .where(ClaimEvidence.evidence_id.in_(evidence_ids))
        ).distinct()
        return list(self.session.scalars(stmt).all())

    # --------------------------------------------------------- structured path

    def retrieve_structured(
        self,
        plan: QueryPlan,
        *,
        use_vector: bool = True,
        candidate_multiplier: int = 4,
        min_vector_score: float = MIN_VECTOR_SCORE,
        source_ids: Sequence[str] | None = None,
    ) -> RetrievalResult:
        """Execute `plan`'s hard constraints, then let FTS/vector rank the survivors.

        The ordering is the invariant. Structured execution runs first and produces the
        qualifying entity set; similarity search is then restricted to the sources behind
        those entities, so it can only reorder and add excerpts. A candidate rejected by
        ``price_per_person < 100`` is not in the allow-list and therefore cannot be
        retrieved by a strong embedding match -- not because a later filter removes it,
        but because it was never a candidate (INTEGRATION_PLAN_V1 §11).

        The reverse arrangement, retrieving first and filtering after, is what makes
        similarity able to override a constraint: anything the filter misses is already in
        the result. Enforcing it by construction rather than by a downstream check is the
        difference between an invariant and a convention.

        The pipeline, in order:

        1. hard structured constraints, over a caller-restricted claim set, produce the
           *full* qualifying entity set (bounded by ``MAX_CANDIDATES``);
        2. FTS/vector runs over the sources behind those entities only;
        3. each qualifying entity takes a relevance score from its own sources' chunks;
        4. the qualifying entities are reordered by that score;
        5. ``plan.limit`` is applied *last*.

        Steps 3-5 are why the limit is not applied in the executor. Truncating before
        ranking would mean similarity orders an arbitrary prefix of the qualifying set --
        whichever rows SQLite returned first -- so the "best" of three shown could be the
        worst three of fifty. `source_ids` is the caller's allow-list and is passed into
        execution, not applied to the output: an entity that qualifies only on a claim from
        an excluded source must never qualify at all, because the answer would then assert
        something no permitted source says.
        """
        executor = StructuredExecutor(self.session, source_ids=source_ids)
        structured = executor.execute(plan)

        diagnostics: dict[str, Any] = {
            "structured": structured.as_dict(),
            "keyword_hits": 0,
            "vector_hits": 0,
            "fts_available": self.keyword.available(),
        }

        if structured.is_empty():
            diagnostics["reason"] = structured.diagnostics.get(
                "reason", "no_structured_matches"
            )
            # `structured` is attached even with no matches, because the rejections *are* the
            # answer on this path: "有匹配的店，但人均 150 超过了 100" is only sayable if the
            # generator can read them. Returning a bare result here would leave the answer
            # generator with nothing but retrieval-level diagnostics and make it report
            # "收藏里没有匹配这个说法的内容" — which is false when a candidate was found and
            # deliberately rejected, and sends the user looking for a video they already have.
            return RetrievalResult(
                query=plan.raw_query, structured=structured, diagnostics=diagnostics
            )

        allowed_sources = structured.qualifying_source_ids
        qualified = len(structured.matches)

        # Over-fetch relative to the *qualifying* set, not to `plan.limit`. Ranking three
        # of fifty survivors requires a score for all fifty; fetching `limit` chunks would
        # leave most entities unscored and sorted by fallback alone.
        support = self.retrieve(
            self._ranking_query(plan),
            limit=max(qualified * 2, plan.limit),
            source_ids=allowed_sources,
            use_vector=use_vector,
            include_claims=False,
            candidate_multiplier=candidate_multiplier,
            min_vector_score=min_vector_score,
        )

        ranked = self._rank_structured_matches(
            structured,
            support.chunks,
            semantic=self._semantic_scores(plan, allowed_sources, support.chunks),
        )
        structured.matches = ranked[: plan.limit]
        structured.refresh_diagnostics()
        structured.diagnostics["qualified"] = qualified
        structured.diagnostics["returned"] = len(structured.matches)

        # Excerpts are narrowed to the entities that survived truncation. Keeping a chunk
        # from a qualifying-but-not-shown entity would offer the answer generator a citation
        # for something the answer does not mention.
        shown_sources = set(structured.qualifying_source_ids)
        chunks = [chunk for chunk in support.chunks if chunk.source_id in shown_sources]

        diagnostics["structured"] = structured.as_dict()
        diagnostics["keyword_hits"] = support.diagnostics.get("keyword_hits", 0)
        diagnostics["vector_hits"] = support.diagnostics.get("vector_hits", 0)
        diagnostics["vector_below_floor"] = support.diagnostics.get("vector_below_floor", 0)
        diagnostics["support_reason"] = support.diagnostics.get("reason")
        diagnostics["fusion"] = (
            "structured_first: hard constraints selected the result set; "
            "FTS/vector only ranked and illustrated it"
        )
        diagnostics["qualified"] = qualified
        diagnostics["returned"] = len(structured.matches)
        diagnostics["ranking"] = structured.diagnostics.get("ranking")
        if source_ids is not None:
            diagnostics["source_restriction"] = len(list(source_ids))

        return RetrievalResult(
            query=plan.raw_query,
            # Claims come from the executor, not from `retrieve`: the executor already
            # selected exactly the claims that satisfied (or conflicted with) a constraint,
            # and re-deriving them from chunks would lose that distinction.
            chunks=chunks,
            claims=structured.claims,
            diagnostics=diagnostics,
            structured=structured,
        )

    @staticmethod
    def _ranking_query(plan: QueryPlan) -> str:
        """The text used for the ranking pass: the question plus its soft requirements.

        Appended rather than substituted, because the question itself remains the primary
        signal. Requirements already present verbatim in the question are not repeated.
        """
        extras = [req for req in plan.semantic_requirements if req not in plan.raw_query]
        if not extras:
            return plan.raw_query
        return plan.raw_query + " " + " ".join(extras)

    def _semantic_scores(
        self,
        plan: QueryPlan,
        allowed_sources: Sequence[str],
        chunks: Sequence[RetrievedChunk],
    ) -> dict[str, float]:
        """Per-source bonus reflecting how well each source supports the *soft* requirements.

        `semantic_requirements` ("适合约会", "安静") are not hard filters in V1: there is no
        structured field behind them and inventing one would mean inventing an ontology,
        which is out of scope. But a requirement that changes nothing is decoration, so they
        get their own ranking pass over the qualifying chunks only. A separate pass rather
        than extra words in the main query, because inside one query the question's own terms
        -- which every qualifying entity matches, since they all satisfy the same district
        and cuisine -- swamp the differentiating term.

        This cannot change membership. It is a score keyed by source id, consulted while
        sorting ``structured.matches``, and an entity absent from that list has nothing for
        it to score.
        """
        if not plan.semantic_requirements or not self.keyword.available():
            return {}
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if not chunk_ids:
            return {}
        source_of = {chunk.chunk_id: chunk.source_id for chunk in chunks}
        scores: dict[str, float] = {}
        hits = self.keyword.search(
            " ".join(plan.semantic_requirements),
            limit=len(chunk_ids),
            doc_types=[DOC_TYPE_CHUNK],
            allowed_object_ids=sorted(chunk_ids),
        )
        for rank, hit in enumerate(hits, start=1):
            source_id = source_of.get(hit.object_id)
            if source_id is None:
                continue
            # Rank-based like RRF, and for the same reason: BM25 magnitudes are not
            # comparable across queries, so only the ordering is trustworthy.
            contribution = 1.0 / (RRF_K + rank)
            if contribution > scores.get(source_id, 0.0):
                scores[source_id] = contribution
        return scores

    @staticmethod
    def _rank_structured_matches(
        structured: StructuredResult,
        chunks: Sequence[RetrievedChunk],
        *,
        semantic: dict[str, float] | None = None,
    ) -> list[StructuredMatch]:
        """Order qualifying entities by the relevance of *their own* sources' chunks.

        The score is the best chunk score among the sources supporting the entity. `max`
        rather than a sum, because a restaurant discussed in one strongly-relevant video
        should not be outranked by one mentioned in passing in four.

        Ties -- including the all-zero case when FTS is unavailable, the vector store is
        absent, or nothing cleared the relevance floor -- fall back to canonical name and
        then entity id. That is arbitrary but *stated* and stable; leaving it to SQLite row
        order would make the same query return a different first result after an unrelated
        reindex, which reads as the system changing its mind.

        This cannot resurrect a rejected candidate: it is a sort over
        ``structured.matches``, and a rejected entity is not in that list. Ranking has no
        route to membership.
        """
        bonus = semantic or {}
        best: dict[str, float] = {}
        for chunk in chunks:
            score = chunk.score + bonus.get(chunk.source_id, 0.0)
            if score > best.get(chunk.source_id, float("-inf")):
                best[chunk.source_id] = score
        # A source may carry a soft-requirement bonus without appearing in the main ranking
        # pass at all; it still has to be able to outrank an unscored peer.
        for source_id, value in bonus.items():
            if value > best.get(source_id, float("-inf")):
                best[source_id] = value

        scored: list[tuple[float, str, str, StructuredMatch]] = []
        for match in structured.matches:
            score = max((best.get(sid, 0.0) for sid in match.source_ids), default=0.0)
            scored.append((score, match.entity.canonical_name, match.entity.id, match))

        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        structured.diagnostics["ranking"] = (
            "similarity_over_qualifying_sources"
            if any(score > 0.0 for score, _, _, _ in scored)
            else "deterministic_fallback_by_name"
        )
        structured.diagnostics["scores"] = {
            match.entity.canonical_name: round(score, 6) for score, _, _, match in scored
        }
        return [match for _, _, _, match in scored]

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
