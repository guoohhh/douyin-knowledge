"""Drives one source through one processing run.

This module owns the two invariants that keep reprocessing honest:

1. **A run is a version, not a mutation.** Processing never edits or deletes the
   output of a previous run. It writes new evidence, mentions, claims and chunks
   under a new ``processing_run_id``, and then moves a pointer.
2. **The pointer is the truth.** ``SourceProcessingState.current_processing_run_id``
   is what retrieval filters on (DB-004). It is advanced *only* after a run
   succeeds, so a crashed or failed run leaves the previous interpretation intact
   and fully queryable. There is no window in which the source has no current
   run because a new one was started.

Levels come from PROCESSING_POLICY: 0 metadata, 1 text, 2 ASR/subtitle,
3 keyframes+OCR, 4 deep multimodal. `achieved_level` records what actually
worked, which can be lower than `target_level` when, say, a video has no audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.text import content_hash, normalize_ws
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    ProcessingRun,
    ProcessingRunEvidence,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.extraction.claim_extractor import ClaimExtractor
from douyin_knowledge.extraction.entity_extractor import EntityExtractor
from douyin_knowledge.extraction.entity_resolver import EntityResolver
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import ASRProvider, StructuredModel
    from douyin_knowledge.config.settings import Settings

logger = get_logger(__name__)

PROCESSOR_VERSION = "1.0.0"
SCHEMA_VERSION = "1"

# Chunking: ~360 chars is a compromise between keeping a whole restaurant
# recommendation in one chunk (so its price and name stay together) and not
# diluting the embedding with unrelated content.
CHUNK_TARGET_CHARS = 360
CHUNK_OVERLAP_CHARS = 60


@dataclass
class ProcessingOutcome:
    """Result of one run, suitable for returning from a job handler."""

    run_id: str
    source_id: str
    status: str
    target_level: int
    achieved_level: int
    evidence_count: int = 0
    mention_count: int = 0
    claim_count: int = 0
    chunk_count: int = 0
    needs_review: int = 0
    error: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "status": self.status,
            "target_level": self.target_level,
            "achieved_level": self.achieved_level,
            "evidence_count": self.evidence_count,
            "mention_count": self.mention_count,
            "claim_count": self.claim_count,
            "chunk_count": self.chunk_count,
            "needs_review": self.needs_review,
            "error": self.error,
            "notes": self.notes,
        }


class ProcessingOrchestrator:
    """Runs the extraction ladder for a single source."""

    def __init__(
        self,
        settings: Settings,
        *,
        structured_model: StructuredModel,
        asr_provider: ASRProvider | None = None,
        resolver: EntityResolver | None = None,
    ) -> None:
        self.settings = settings
        self.structured_model = structured_model
        self.asr_provider = asr_provider
        self.entity_extractor = EntityExtractor(structured_model)
        self.claim_extractor = ClaimExtractor(structured_model)
        self.resolver = resolver or EntityResolver()

    # ------------------------------------------------------------------ public

    def process_source(
        self, session: Session, source_id: str, *, target_level: int = 2
    ) -> ProcessingOutcome:
        """Process one source end to end and advance its currency pointer."""
        source = session.get(Source, source_id)
        if source is None:
            raise ValueError(f"unknown source_id: {source_id}")

        run = ProcessingRun(
            source_id=source_id,
            run_kind="extract",
            processor_version=PROCESSOR_VERSION,
            schema_version=SCHEMA_VERSION,
            target_level=target_level,
            achieved_level=0,
            status="running",
            models_json=self._models_used(),
            config_json={
                "chunk_target_chars": CHUNK_TARGET_CHARS,
                "chunk_overlap_chars": CHUNK_OVERLAP_CHARS,
            },
            started_at_ms=now_ms(),
        )
        session.add(run)
        session.flush()

        outcome = ProcessingOutcome(
            run_id=run.id,
            source_id=source_id,
            status="running",
            target_level=target_level,
            achieved_level=0,
        )

        try:
            evidence, achieved = self._build_evidence(session, source, run, target_level, outcome)
            outcome.evidence_count = len(evidence)
            outcome.achieved_level = achieved
            run.achieved_level = achieved

            if evidence:
                mentions = self.entity_extractor.extract_from_evidence(
                    session, evidence, source_id=source_id, processing_run_id=run.id
                )
                outcome.mention_count = len(mentions)

                outcomes = self.resolver.resolve_batch(session, mentions)
                outcome.needs_review = sum(1 for o in outcomes if o.needs_review)

                creator_name = source.creator.display_name if source.creator else None
                claims = self.claim_extractor.extract_from_evidence(
                    session,
                    evidence,
                    source_id=source_id,
                    processing_run_id=run.id,
                    mentions=mentions,
                    creator_name=creator_name,
                )
                outcome.claim_count = len(claims)

                chunks = self._build_chunks(session, source, run, evidence)
                outcome.chunk_count = len(chunks)
            else:
                outcome.notes.append("no_usable_evidence")

            run.status = "succeeded"
            run.finished_at_ms = now_ms()
            outcome.status = "succeeded"

            # Only now does this run become the one retrieval can see.
            self._advance_currency(session, source_id, run)

        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            run.status = "failed"
            run.finished_at_ms = now_ms()
            run.error_json = {"type": type(exc).__name__, "message": str(exc)}
            outcome.status = "failed"
            outcome.error = run.error_json
            self._record_failure(session, source_id, run.error_json)
            logger.exception(
                "processing_run_failed", extra={"source_id": source_id, "run_id": run.id}
            )
            raise

        logger.info("processing_run_succeeded", extra=outcome.as_dict())
        return outcome

    # ---------------------------------------------------------------- evidence

    def _build_evidence(
        self,
        session: Session,
        source: Source,
        run: ProcessingRun,
        target_level: int,
        outcome: ProcessingOutcome,
    ) -> tuple[list[EvidenceUnit], int]:
        """Produce evidence units per the level ladder.

        Each level is attempted only if the previous one succeeded, and the
        highest level that produced usable evidence becomes `achieved_level`.
        """
        units: list[EvidenceUnit] = []
        achieved = 0

        # Level 0-1: metadata and creator-authored text. Always available.
        for kind, text in (("title", source.title), ("caption", source.caption_raw)):
            unit = self._make_evidence(session, source.id, run, kind, text)
            if unit is not None:
                units.append(unit)
                achieved = max(achieved, 1)

        # Level 2: spoken content. Prefer a native subtitle when the platform
        # gave us one — it is more accurate than ASR and costs nothing.
        if target_level >= 2:
            subtitle = self._native_subtitle(session, source.id)
            if subtitle is not None:
                units.append(subtitle)
                achieved = max(achieved, 2)
                outcome.notes.append("used_native_subtitle")
            elif self.asr_provider is not None:
                transcribed = self._run_asr(session, source, run, outcome)
                if transcribed:
                    units.extend(transcribed)
                    achieved = max(achieved, 2)
            else:
                outcome.notes.append("level2_skipped_no_audio_source")

        if target_level >= 3:
            # Keyframe extraction and OCR need ffmpeg plus a vision provider;
            # not wired here, and claiming level 3 without doing the work would
            # corrupt the policy ladder's idea of what has been done.
            outcome.notes.append("level3_not_implemented")

        for unit in units:
            session.add(
                ProcessingRunEvidence(
                    processing_run_id=run.id, evidence_id=unit.id, usage_role="produced"
                )
            )
        return units, achieved

    def _make_evidence(
        self, session: Session, source_id: str, run: ProcessingRun, kind: str, text: str | None
    ) -> EvidenceUnit | None:
        """Create an evidence unit, reusing an identical one if it exists.

        Evidence is deduplicated by (source, kind, content_hash) at the table
        level. Identical text across runs is the *same observation*, so the row
        is shared rather than duplicated; the run linkage lives in
        ``ProcessingRunEvidence``.
        """
        normalized = normalize_ws(text or "")
        if not normalized:
            return None

        digest = content_hash(kind, normalized)
        existing = session.scalars(
            select(EvidenceUnit).where(
                EvidenceUnit.source_id == source_id,
                EvidenceUnit.kind == kind,
                EvidenceUnit.content_hash == digest,
            )
        ).first()
        if existing is not None:
            return existing

        unit = EvidenceUnit(
            source_id=source_id,
            kind=kind,
            raw_text=text,
            normalized_text=normalized,
            content_hash=digest,
            language="zh",
            confidence=1.0,
        )
        session.add(unit)
        session.flush()
        return unit

    def _native_subtitle(self, session: Session, source_id: str) -> EvidenceUnit | None:
        """Reuse the subtitle evidence written at sync time.

        Provider-supplied subtitles are recorded as evidence by
        `CaptureSyncService`, so processing adopts that observation rather than
        re-deriving it. Evidence is run-independent; the run linkage is what
        makes it part of *this* interpretation.
        """
        return session.scalars(
            select(EvidenceUnit)
            .where(
                EvidenceUnit.source_id == source_id,
                EvidenceUnit.kind == "subtitle",
            )
            .order_by(EvidenceUnit.created_at_ms.desc())
        ).first()

    def _run_asr(
        self, session: Session, source: Source, run: ProcessingRun, outcome: ProcessingOutcome
    ) -> list[EvidenceUnit]:
        assert self.asr_provider is not None
        audio = session.scalars(
            select(SourceAsset).where(
                SourceAsset.source_id == source.id,
                SourceAsset.asset_type.in_(["audio", "video"]),
            )
        ).first()
        if audio is None:
            outcome.notes.append("asr_skipped_no_media")
            return []

        # storage_key is a local path only once the media has been downloaded;
        # a remote URL means the fetch step has not run, so ASR is not possible.
        local = self.settings.media_dir / audio.storage_key
        if not local.exists():
            outcome.notes.append("asr_skipped_media_not_downloaded")
            return []

        try:
            response = self.asr_provider.transcribe(str(local))
        except Exception as exc:  # noqa: BLE001 - a missing transcript is not a run failure
            logger.warning("asr_failed", extra={"source_id": source.id, "error": str(exc)})
            outcome.notes.append("asr_failed")
            return []

        units: list[EvidenceUnit] = []
        for segment in response.segments:
            text = normalize_ws(segment.text)
            if not text:
                continue
            digest = content_hash("asr", text, str(segment.start_ms))
            unit = EvidenceUnit(
                source_id=source.id,
                asset_id=audio.id,
                kind="asr",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                raw_text=segment.text,
                normalized_text=text,
                content_hash=digest,
                language=response.language or "zh",
                confidence=getattr(segment, "confidence", None) or 0.8,
            )
            session.add(unit)
            units.append(unit)
        session.flush()
        return units

    # ------------------------------------------------------------------ chunks

    def _build_chunks(
        self,
        session: Session,
        source: Source,
        run: ProcessingRun,
        evidence: list[EvidenceUnit],
    ) -> list[RetrievalChunk]:
        """Group evidence into retrieval-sized chunks that remember their sources.

        Chunks are built per kind so a caption never merges with a subtitle: they
        have different reliability, and a citation must be traceable to one of
        them specifically.
        """
        by_kind: dict[str, list[EvidenceUnit]] = {}
        for unit in evidence:
            by_kind.setdefault(unit.kind, []).append(unit)

        chunks: list[RetrievalChunk] = []
        ordinal = 0
        for kind, units in by_kind.items():
            for group in self._group_units(units):
                text = normalize_ws(" ".join(u.normalized_text or "" for u in group))
                if not text:
                    continue
                starts = [u.start_ms for u in group if u.start_ms is not None]
                ends = [u.end_ms for u in group if u.end_ms is not None]
                chunk = RetrievalChunk(
                    source_id=source.id,
                    processing_run_id=run.id,
                    chunk_type=kind,
                    ordinal=ordinal,
                    text=text,
                    start_ms=min(starts) if starts else None,
                    end_ms=max(ends) if ends else None,
                    content_hash=content_hash(kind, text),
                )
                session.add(chunk)
                session.flush()
                for unit in group:
                    session.add(
                        RetrievalChunkEvidence(
                            retrieval_chunk_id=chunk.id, evidence_id=unit.id
                        )
                    )
                chunks.append(chunk)
                ordinal += 1
        return chunks

    @staticmethod
    def _group_units(units: list[EvidenceUnit]) -> list[list[EvidenceUnit]]:
        """Pack consecutive units up to the target size, never splitting one.

        Evidence units are the citation atoms, so a chunk boundary must fall
        between them. An oversized single unit becomes its own chunk rather than
        being cut in half.
        """
        groups: list[list[EvidenceUnit]] = []
        current: list[EvidenceUnit] = []
        length = 0
        for unit in units:
            size = len(unit.normalized_text or "")
            if current and length + size > CHUNK_TARGET_CHARS:
                groups.append(current)
                # Carry the last unit forward as overlap so a fact spanning a
                # boundary stays retrievable from both sides.
                if size < CHUNK_OVERLAP_CHARS:
                    current = [current[-1], unit]
                    length = len(current[0].normalized_text or "") + size
                else:
                    current = [unit]
                    length = size
                continue
            current.append(unit)
            length += size
        if current:
            groups.append(current)
        return groups

    # ------------------------------------------------------------------- state

    def _advance_currency(self, session: Session, source_id: str, run: ProcessingRun) -> None:
        state = session.get(SourceProcessingState, source_id)
        if state is None:
            state = SourceProcessingState(source_id=source_id)
            session.add(state)
        state.current_processing_run_id = run.id
        state.achieved_level = run.achieved_level
        state.processing_status = "processed"
        state.last_success_at_ms = now_ms()
        state.last_error_json = None
        session.flush()

    def _record_failure(
        self, session: Session, source_id: str, error: dict[str, Any]
    ) -> None:
        """Record the error without touching the currency pointer.

        The previous run stays current, so a failed reprocess degrades to "no
        new knowledge" rather than "this source is now unsearchable".
        """
        state = session.get(SourceProcessingState, source_id)
        if state is None:
            state = SourceProcessingState(source_id=source_id)
            session.add(state)
        state.processing_status = "failed"
        state.last_error_json = error
        session.flush()

    def _models_used(self) -> dict[str, Any]:
        """Provenance for this run. Records the resolved model *names* alongside the
        adapter classes: the class says which code path ran, the name says which model
        produced the claims, and a re-read of the run needs both to be reproducible."""
        return {
            "provider": getattr(self.settings, "ai_provider", "mock"),
            "extraction_model": self.settings.model_for_role("extraction"),
            "asr_model": (
                self.settings.model_for_role("asr") if self.asr_provider else None
            ),
            "structured": type(self.structured_model).__name__,
            "asr": type(self.asr_provider).__name__ if self.asr_provider else None,
            "processor_version": PROCESSOR_VERSION,
        }


__all__ = ["PROCESSOR_VERSION", "SCHEMA_VERSION", "ProcessingOrchestrator", "ProcessingOutcome"]
