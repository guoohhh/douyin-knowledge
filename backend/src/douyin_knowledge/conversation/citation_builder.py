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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.text import truncate
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.conversation import MessageCitation
from douyin_knowledge.db.models.processing import EvidenceUnit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.db.models.entities import Claim
    from douyin_knowledge.retrieval.retriever import RetrievalResult, RetrievedChunk

SNIPPET_LIMIT = 120


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
    """Ordered citations plus lookups the generator uses to place markers."""

    citations: list[Citation] = field(default_factory=list)
    by_chunk: dict[str, Citation] = field(default_factory=dict)
    by_claim: dict[str, Citation] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.citations)

    def as_list(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.citations]


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
            ordinal += 1
            citation = self._citation_for_chunk(
                chunk, ordinal, source_titles.get(chunk.source_id), evidence_map
            )
            citation_set.citations.append(citation)
            citation_set.by_chunk[chunk.chunk_id] = citation
            if citation.evidence_id:
                evidence_to_citation[citation.evidence_id] = citation

        claim_evidence = self._claim_evidence_map([c.id for c in result.claims])
        for claim in result.claims:
            existing = None
            for evidence_id in claim_evidence.get(claim.id, []):
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

            if ordinal >= limit:
                break
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

    def _citation_for_chunk(
        self,
        chunk: RetrievedChunk,
        ordinal: int,
        source_title: str | None,
        evidence_map: dict[str, EvidenceUnit],
    ) -> Citation:
        """Cite the most precise level this chunk's evidence supports."""
        evidence_id: str | None = None
        start_ms = chunk.start_ms
        snippet = truncate(chunk.text, SNIPPET_LIMIT)
        precision = "source"

        for candidate_id in chunk.evidence_ids:
            unit = evidence_map.get(candidate_id)
            if unit is None:
                continue
            evidence_id = unit.id
            precision = "evidence"
            if unit.start_ms is not None:
                # Timestamped evidence is the top of the hierarchy; prefer it
                # and stop looking.
                start_ms = unit.start_ms
                precision = "evidence_timestamp"
                snippet = truncate(unit.normalized_text or unit.raw_text or snippet, SNIPPET_LIMIT)
                break
            snippet = truncate(unit.normalized_text or unit.raw_text or snippet, SNIPPET_LIMIT)

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
