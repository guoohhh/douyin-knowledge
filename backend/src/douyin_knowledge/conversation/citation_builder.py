"""Citation assembly: retrieval results -> ``message_citations`` rows.

RETRIEVAL.md 14 defines a precision hierarchy for citations:

    source + timestamp + evidence  >  source + evidence excerpt  >  source only

This module always cites at the most precise level the retrieval actually
supports, and never above it. That asymmetry is deliberate — a citation that
looks more precise than the underlying evidence is worse than no citation,
because the user stops checking.

The hard rule from RETRIEVAL.md 14 ("No fake provenance") is enforced
structurally rather than by convention: ``CitationBuilder`` can only mint a
citation from a retrieval object it was handed, so there is no code path where
the answer generator invents one. General-knowledge statements get no citation
at all, which is why hybrid answers keep their two halves visually separate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.text import truncate
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.conversation import MessageCitation
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.search.tokenizer import strip_query_stopwords, tokenize

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.db.models.entities import Claim
    from douyin_knowledge.retrieval.retriever import RetrievalResult, RetrievedChunk

logger = get_logger(__name__)

SNIPPET_LIMIT = 120

MAX_EVIDENCE_PER_CHUNK = 3
"""Most citations one retrieved chunk may mint.

A chunk is a retrieval context, not a citation target, and a broad one can link dozens of
evidence units -- the real 手抓饼 ASR chunk links 56 across 2m22s. Projecting all of them
would blow the citation budget on one source and hand the model a transcript to summarize
instead of evidence to cite. Three is the smallest window that holds a question and its
answer with one unit of context around them, which is the shape the failure needed:
``手抓饼。/ 多少钱？/ 八元。``
"""

EVIDENCE_LOOKAHEAD = 2
"""Units after the last anchor that may join the window.

Asymmetric with the lookback on purpose. In speech the answer follows the question, so the
unit that *supports* a fact is usually downstream of the unit that lexically matches the
query: 多少钱？ matches, 八元。 answers.
"""

EVIDENCE_LOOKBACK = 1
"""Units before the first anchor that may join the window.

One, not zero, because an anchor can be the answer rather than the question -- a bare 八元。
means nothing without 手抓饼。 in front of it -- and not more, because each extra unit is
budget spent on context rather than on another source.
"""


def _text_of(unit: EvidenceUnit) -> str:
    return (unit.normalized_text or unit.raw_text or "").strip()


def _overlap(query: str, text: str) -> int:
    """How many distinct query terms this evidence text contains.

    Distinct, not total: a unit that says 钱钱钱 is not three times the evidence of one that
    says 钱. Segmentation is the same `tokenize` the FTS index and the MATCH query both run
    through, so "relevant to the query" means the same thing here as it did during retrieval
    -- a second notion of relevance would let the projection disagree with the ranker about
    why a chunk was retrieved.
    """
    if not text:
        return 0
    terms = set(strip_query_stopwords(tokenize(query)))
    if not terms:
        return 0
    return len(terms & set(tokenize(text)))


def _best_anchor_cluster(scores: list[int]) -> list[int]:
    """Indices of the strongest run of consecutive query-relevant units.

    A long transcript mentions the query's words in several places: 手抓饼多少钱 matches
    both 多少钱一份 (about 烤冷面, early) and 手抓饼。/ 多少钱？ (the real passage). Scoring
    units independently and taking the single best one picks between them on a one-token
    margin. Summing over consecutive matches instead prefers the passage where the query's
    terms actually converge, which is the passage the user is asking about.

    Ties go to the earlier cluster, so the result depends only on the scores.
    """
    clusters: list[list[int]] = []
    for index, score in enumerate(scores):
        if score == 0:
            continue
        if clusters and clusters[-1][-1] == index - 1:
            clusters[-1].append(index)
        else:
            clusters.append([index])
    return max(clusters, key=lambda c: (sum(scores[i] for i in c), -c[0]))


def _window_indices(anchors: list[int], scores: list[int], total: int) -> list[int]:
    """The bounded window around `anchors`, as sorted indices.

    Priority when `MAX_EVIDENCE_PER_CHUNK` cannot hold everything: anchors first (strongest,
    then earliest), then lookahead units nearest the cluster, then lookback. Anchors come
    first because they are the units with demonstrated relevance to the query; lookahead
    beats lookback because in speech the answer follows the question.
    """
    ranked: list[int] = sorted(anchors, key=lambda i: (-scores[i], i))
    for offset in range(1, EVIDENCE_LOOKAHEAD + 1):
        candidate = anchors[-1] + offset
        if candidate < total and candidate not in ranked:
            ranked.append(candidate)
    for offset in range(1, EVIDENCE_LOOKBACK + 1):
        candidate = anchors[0] - offset
        if candidate >= 0 and candidate not in ranked:
            ranked.append(candidate)
    return sorted(ranked[:MAX_EVIDENCE_PER_CHUNK])


def format_timestamp(ms: int | None) -> str | None:
    """mm:ss for a citation label. Users locate evidence by time, not by ms."""
    if ms is None:
        return None
    total_seconds = max(0, ms // 1000)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


@dataclass
class Citation:
    """One resolved citation, before it becomes a DB row."""

    ordinal: int
    source_id: str | None = None
    evidence_id: str | None = None
    claim_id: str | None = None
    wiki_page_id: str | None = None
    label: str | None = None
    source_title: str | None = None
    start_ms: int | None = None
    snippet: str | None = None
    precision: str = "source"

    @property
    def marker(self) -> str:
        return f"[{self.ordinal}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "marker": self.marker,
            "source_id": self.source_id,
            "evidence_id": self.evidence_id,
            "claim_id": self.claim_id,
            "wiki_page_id": self.wiki_page_id,
            "label": self.label,
            "source_title": self.source_title,
            "start_ms": self.start_ms,
            "timestamp": format_timestamp(self.start_ms),
            "snippet": self.snippet,
            "precision": self.precision,
        }


@dataclass
class CitationSet:
    """Ordered citations plus lookups the generator uses to place markers.

    This set is also the *authority on what may be asserted*. Retrieval relevance and
    user-assertability are different questions: a claim can be worth ranking and worth
    keeping in diagnostics while being impossible to attribute to anything the user could
    check. The `renderable_*` helpers below answer the second question, and they live here
    because this class minted the provenance -- asking each renderer to remember
    ``if citation is not None`` is what produced the Stage 3C failure in four places at once.
    """

    citations: list[Citation] = field(default_factory=list)
    chunk_windows: dict[str, list[Citation]] = field(default_factory=dict)
    by_claim: dict[str, Citation] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.citations)

    def as_list(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.citations]

    # -------------------------------------------------- renderable knowledge

    def can_cite_claim(self, claim_id: str) -> bool:
        """Whether this claim may be asserted to the user at all."""
        return claim_id in self.by_claim

    def can_cite_chunk(self, chunk_id: str) -> bool:
        return bool(self.chunk_windows.get(chunk_id))

    def citations_for_chunk(self, chunk_id: str) -> list[Citation]:
        """Every citation projected from one retrieved chunk, in evidence order.

        Plural, and named to say so. This replaced a ``by_chunk: dict[str, Citation]``
        whose single value was the whole Stage 3C projection failure: a chunk linking 56
        evidence units was represented by one of them, and the renderers then printed that
        one unit's text as if it were the chunk's content. The model was handed
        ``[1]（语音转写 @ 01:55）补钙啊？`` for the query 手抓饼多少钱 -- correct retrieval,
        destroyed provenance.

        A caller wanting "the chunk's text" wants `chunk.text`. A caller wanting provenance
        wants all of these, because each one points at a different moment and only some of
        them support any given fact. There is deliberately no ``primary`` accessor: picking
        one for the caller is what made a neighbouring line's timestamp look like proof of
        an unrelated claim.
        """
        return list(self.chunk_windows.get(chunk_id, ()))

    def renderable_claims(self, claims: Iterable[Any]) -> list[Any]:
        """The subset of `claims` that carries provenance in this set.

        Callers should filter *before* truncating to a display budget. Filtering after
        would spend display slots on claims that are then dropped, so a citable fact could
        lose its place to an uncitable one.
        """
        return [claim for claim in claims if self.can_cite_claim(claim.id)]

    def renderable_chunks(self, chunks: Iterable[Any]) -> list[Any]:
        return [chunk for chunk in chunks if self.can_cite_chunk(chunk.chunk_id)]


class CitationBuilder:
    """Resolve retrieval output into citations and persist them."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ----------------------------------------------------------- resolution

    def _source_titles(self, source_ids: Sequence[str]) -> dict[str, str | None]:
        if not source_ids:
            return {}
        rows = self.session.execute(
            select(Source.id, Source.title).where(Source.id.in_(list(source_ids)))
        )
        return {row[0]: row[1] for row in rows}

    def _evidence(self, evidence_ids: Sequence[str]) -> dict[str, EvidenceUnit]:
        if not evidence_ids:
            return {}
        units = self.session.scalars(
            select(EvidenceUnit).where(EvidenceUnit.id.in_(list(evidence_ids)))
        ).all()
        return {unit.id: unit for unit in units}

    def build(self, result: RetrievalResult, *, limit: int = 12) -> CitationSet:
        """Build the citation set for one retrieval result.

        Chunks are cited first and in retrieval order, so marker numbers follow
        relevance. Claims that rest on already-cited evidence reuse that
        citation instead of minting a duplicate: two markers pointing at the same
        moment of the same video reads as two independent confirmations, which
        would overstate the support.

        A chunk projects to a *window* of its evidence rather than to one unit; see
        :meth:`_project_chunk`. The budget is unchanged and still counts citations, not
        chunks, so a broad chunk that earns three markers leaves nine for everything else.
        """
        citation_set = CitationSet()
        ordinal = 0

        source_titles = self._source_titles(result.source_ids)
        all_evidence_ids = [eid for chunk in result.chunks for eid in chunk.evidence_ids]
        evidence_map = self._evidence(all_evidence_ids)
        evidence_to_citation: dict[str, Citation] = {}

        for chunk in result.chunks:
            if ordinal >= limit:
                break
            window = self._project_chunk(chunk, result.query, evidence_map)
            minted: list[Citation] = []
            for unit in window:
                existing = evidence_to_citation.get(unit.id)
                if existing is not None:
                    # Already cited, by an earlier chunk that links the same unit. Reuse the
                    # ordinal instead of minting a second one, for the reason claim reuse
                    # exists: two markers on one moment of one video reads as two
                    # independent confirmations. Overlapping chunks over a shared transcript
                    # are the common case, not the exception -- a title chunk, a caption
                    # chunk and an ASR chunk of one source routinely resolve to the same
                    # units -- so minting per chunk also spent the whole budget restating
                    # one source.
                    minted.append(existing)
                    continue
                if ordinal >= limit:
                    break
                ordinal += 1
                citation = self._citation_for_evidence(
                    chunk, unit, ordinal, source_titles.get(chunk.source_id)
                )
                citation_set.citations.append(citation)
                minted.append(citation)
                if citation.evidence_id:
                    evidence_to_citation[citation.evidence_id] = citation
            if not window:
                # Every evidence id on the chunk failed to resolve to a row. The chunk is
                # still real and still retrieved, so it is cited at source precision --
                # the bottom of the RETRIEVAL.md 14 hierarchy, which is what "we know
                # which video, not which moment" honestly looks like.
                ordinal += 1
                citation = self._citation_for_evidence(
                    chunk, None, ordinal, source_titles.get(chunk.source_id)
                )
                citation_set.citations.append(citation)
                minted.append(citation)
            citation_set.chunk_windows[chunk.chunk_id] = minted

        claim_evidence = self._claim_evidence_map([c.id for c in result.claims])
        for claim in result.claims:
            # No same-source evidence at all means the claim is not citable, and minting a
            # claim-precision citation anyway is how the crossed row came back after
            # `_claim_evidence_map` had just dropped it: the map removed source B's evidence,
            # the reuse lookup found nothing, and the fallback below asserted the claim on
            # its own authority -- a citation whose precision field says "claim" with no
            # evidence behind it. Failing closed here is the last gate before render.
            #
            # This is *not* the same as "none of its evidence was retrieved". A healthy claim
            # whose same-source evidence exists but was not among the retrieved chunks has a
            # non-empty entry here and still earns the fallback citation below; that is the
            # ordinary source-level case and it is unchanged.
            citable_evidence = claim_evidence.get(claim.id)
            if not citable_evidence:
                logger.warning(
                    "claim_citation_suppressed",
                    extra={"claim_id": claim.id, "claim_source": claim.source_id},
                )
                continue

            existing = None
            for evidence_id in citable_evidence:
                existing = evidence_to_citation.get(evidence_id)
                if existing is not None:
                    break
            if existing is not None:
                # Attach the claim to the evidence citation already shown; this
                # upgrades that citation's precision rather than adding noise.
                if existing.claim_id is None:
                    existing.claim_id = claim.id
                citation_set.by_claim[claim.id] = existing
                continue

            # `continue`, not `break`. Minting is budgeted, but *reuse* costs no ordinal, so
            # a claim that cannot mint must not end the loop -- the claims after it may still
            # attach to citations already minted above, for free. With `break` here, one
            # unreusable claim early in the list denied provenance to every later claim: on
            # the real Agent-learning shape, 14 claims with 12 of their evidence units
            # already cited produced a completely empty `by_claim`, and the renderers then
            # asserted all 14 of them with no marker.
            if ordinal >= limit:
                continue
            ordinal += 1
            citation = Citation(
                ordinal=ordinal,
                source_id=claim.source_id,
                claim_id=claim.id,
                source_title=source_titles.get(claim.source_id),
                label=self._claim_label(claim, source_titles.get(claim.source_id)),
                precision="claim",
            )
            citation_set.citations.append(citation)
            citation_set.by_claim[claim.id] = citation

        return citation_set

    def _claim_evidence_map(self, claim_ids: Sequence[str]) -> dict[str, list[str]]:
        """Evidence ids per claim, restricted to evidence from the claim's own source.

        The source check lives here rather than in :meth:`build` because this map is the
        single place every consumer reads the claim-evidence spine from: the reuse lookup
        that upgrades a chunk citation to an evidence-precise one, and the ``by_claim``
        mapping the structured renderer quotes from. Filtering here drops a crossed row
        once instead of asking each caller to re-check provenance, and a caller that
        forgot would cite source A's claim with source B's words.
        """
        if not claim_ids:
            return {}
        from douyin_knowledge.db.models.entities import Claim, ClaimEvidence

        rows = self.session.execute(
            select(ClaimEvidence.claim_id, ClaimEvidence.evidence_id)
            .join(Claim, Claim.id == ClaimEvidence.claim_id)
            .join(EvidenceUnit, EvidenceUnit.id == ClaimEvidence.evidence_id)
            .where(
                ClaimEvidence.claim_id.in_(list(claim_ids)),
                EvidenceUnit.source_id == Claim.source_id,
            )
        )
        mapping: dict[str, list[str]] = {}
        for claim_id, evidence_id in rows:
            mapping.setdefault(claim_id, []).append(evidence_id)
        return mapping

    def _project_chunk(
        self,
        chunk: RetrievedChunk,
        query: str,
        evidence_map: dict[str, EvidenceUnit],
    ) -> list[EvidenceUnit]:
        """The query-relevant evidence window of one retrieved chunk.

        ``RetrievalChunk != EvidenceUnit``. Retrieval may legitimately rank a broad context
        -- a whole ASR pass over a 2m22s video -- but every fact shown to the user has to
        cite the specific unit that supports it. This is the bridge: an ordered chunk of
        many units in, a small ordered window of units out, each of which becomes its own
        citation.

        Three things it deliberately is not:

        *Not "the first unit".* That is the bug being fixed. With SQL row order it was
        arbitrary; with time order it would be the video's opening line.

        *Not "the unit with the highest lexical overlap".* For 手抓饼多少钱 the strongest
        overlap is 手抓饼。 or 多少钱？ -- the restatement of the question. The answer is
        the *next* unit, 八元。, which shares no token with the query at all. Overlap
        locates the anchor; the window is what actually contains the answer.

        *Not unbounded.* See `MAX_EVIDENCE_PER_CHUNK`. Fewer precisely grounded units beat
        many loosely related ones, and the citation budget is shared with every other
        source.
        """
        units = self._ordered_units(chunk, evidence_map)
        if len(units) <= 1:
            return units

        scores = [_overlap(query, _text_of(unit)) for unit in units]
        if not any(scores):
            # No lexical signal anywhere -- the chunk was retrieved by the vector arm, or by
            # tokens that live in its title rather than its transcript. Anchoring on the
            # first unit is honest here in a way it never was above: there is no evidence
            # that any other unit is more relevant, and a single earliest-unit citation is
            # exactly the pre-existing behavior for chunks with nothing to choose between.
            return units[:1]

        anchors = _best_anchor_cluster(scores)
        return [units[index] for index in _window_indices(anchors, scores, len(units))]

    @staticmethod
    def _ordered_units(
        chunk: RetrievedChunk, evidence_map: dict[str, EvidenceUnit]
    ) -> list[EvidenceUnit]:
        """Resolvable units of a chunk, deterministically ordered and de-duplicated.

        Sorted here as well as in the retriever's SQL: this list decides which timestamps a
        user is shown, and it must not depend on a caller having ordered its input or on
        ``IN (...)`` row order. Units with no timestamp sort after timestamped ones, by id,
        so a mixed chunk is still stable.

        Identical text collapses to one unit. Two rows with the same words are the same
        excerpt however many claims extracted it, and citing both would show the user one
        sentence twice under two markers -- which reads as two independent confirmations.
        """
        units = [
            unit
            for unit in (evidence_map.get(eid) for eid in chunk.evidence_ids)
            if unit is not None
        ]
        units.sort(key=lambda u: (u.start_ms is None, u.start_ms or 0, u.end_ms or 0, u.id))
        seen: set[str] = set()
        deduped: list[EvidenceUnit] = []
        for unit in units:
            key = _text_of(unit)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            deduped.append(unit)
        return deduped

    def _citation_for_evidence(
        self,
        chunk: RetrievedChunk,
        unit: EvidenceUnit | None,
        ordinal: int,
        source_title: str | None,
    ) -> Citation:
        """Cite one evidence unit at the most precise level it supports.

        ``unit is None`` is the degenerate case where a chunk's evidence links all dangled;
        it yields the source-precision citation this method used to produce as a fallback
        inside a loop over candidates.
        """
        start_ms = chunk.start_ms
        snippet = truncate(chunk.text, SNIPPET_LIMIT)
        evidence_id: str | None = None
        precision = "source"

        if unit is not None:
            evidence_id = unit.id
            precision = "evidence"
            snippet = truncate(_text_of(unit) or snippet, SNIPPET_LIMIT)
            if unit.start_ms is not None:
                start_ms = unit.start_ms
                precision = "evidence_timestamp"

        timestamp = format_timestamp(start_ms)
        title = source_title or "未命名来源"
        label = f"{title} @ {timestamp}" if timestamp else title

        return Citation(
            ordinal=ordinal,
            source_id=chunk.source_id,
            evidence_id=evidence_id,
            label=label,
            source_title=source_title,
            start_ms=start_ms,
            snippet=snippet,
            precision=precision,
        )

    @staticmethod
    def _claim_label(claim: Claim, source_title: str | None) -> str:
        title = source_title or "未命名来源"
        return f"{title} · {claim.predicate}"

    # ---------------------------------------------------------- persistence

    def persist(self, message_id: str, citation_set: CitationSet) -> int:
        """Write citations for a message.

        Ordinals are stored so the markers in ``message.content`` stay
        meaningful after a reload — regenerating them from retrieval later would
        renumber the answer and break every reference inside its own text.
        """
        for citation in citation_set.citations:
            self.session.add(
                MessageCitation(
                    message_id=message_id,
                    ordinal=citation.ordinal,
                    source_id=citation.source_id,
                    evidence_id=citation.evidence_id,
                    claim_id=citation.claim_id,
                    wiki_page_id=citation.wiki_page_id,
                    label=citation.label,
                )
            )
        return len(citation_set.citations)

    def load(self, message_id: str) -> list[dict[str, Any]]:
        """Rehydrate stored citations for display."""
        rows = self.session.scalars(
            select(MessageCitation)
            .where(MessageCitation.message_id == message_id)
            .order_by(MessageCitation.ordinal)
        ).all()
        source_titles = self._source_titles([r.source_id for r in rows if r.source_id])
        evidence_map = self._evidence([r.evidence_id for r in rows if r.evidence_id])

        out: list[dict[str, Any]] = []
        for row in rows:
            unit = evidence_map.get(row.evidence_id) if row.evidence_id else None
            out.append(
                {
                    "ordinal": row.ordinal,
                    "marker": f"[{row.ordinal}]",
                    "source_id": row.source_id,
                    "evidence_id": row.evidence_id,
                    "claim_id": row.claim_id,
                    "wiki_page_id": row.wiki_page_id,
                    "label": row.label,
                    "source_title": source_titles.get(row.source_id) if row.source_id else None,
                    "start_ms": unit.start_ms if unit else None,
                    "timestamp": format_timestamp(unit.start_ms) if unit else None,
                    "snippet": truncate(
                        (unit.normalized_text or unit.raw_text or ""), SNIPPET_LIMIT
                    )
                    if unit
                    else None,
                }
            )
        return out


__all__ = ["Citation", "CitationBuilder", "CitationSet", "format_timestamp"]
