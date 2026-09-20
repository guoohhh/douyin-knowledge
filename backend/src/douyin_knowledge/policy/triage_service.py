"""Caching layer around `CheapTriage`: classify a source once, reuse it until it changes.

Separate from `triage.py` so the classifier stays a pure text function with no database
in it. This module owns the two things that need a session: reading a cached label, and
deciding when a cached label has gone stale.

Staleness is keyed on `SourceSignal.fingerprint` rather than a timestamp. A sync that
returns byte-identical metadata is the common case -- it happens every time the user syncs
-- and re-running triage on it would be free for cues and billed for the model fallback,
so time-based expiry would charge for nothing. Conversely a creator who edits a title from
"香港美食探店" to "综艺现场" has genuinely changed what the video claims to be, and that
must reclassify regardless of how recently it was last done.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.policy import SourceTriage
from douyin_knowledge.policy.signal import SourceSignal, build_signal
from douyin_knowledge.policy.triage import (
    CheapTriage,
    ContentType,
    TriageMethod,
    TriageResult,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from douyin_knowledge.db.models.capture import Source


class TriageService:
    """Classify persisted sources, caching the result in `source_triage`."""

    def __init__(self, session: Session, triage: CheapTriage | None = None) -> None:
        self.session = session
        self.triage = triage or CheapTriage()

    # ------------------------------------------------------------------ public

    def classify(
        self,
        source: Source,
        *,
        allow_model: bool = False,
        force: bool = False,
    ) -> TriageResult:
        """Return the content type for `source`, computing it if needed.

        A cached row whose fingerprint still matches is returned as-is, with one
        exception: a cached `unknown` that was reached *without* a model is recomputed
        when `allow_model` is now true. Otherwise the first inconclusive pass would pin
        the source as unknown forever, and configuring a provider later would have no
        effect on anything already synced.
        """
        signal = build_signal(self.session, source)
        cached = self.session.get(SourceTriage, source.id)

        if not force and cached is not None and cached.signal_fingerprint == signal.fingerprint:
            result = _row_to_result(cached)
            escalatable = (
                result.content_type is ContentType.UNKNOWN
                and result.method is not TriageMethod.MODEL
            )
            if not (allow_model and escalatable and self.triage.model is not None):
                return result

        result = self.triage.classify(signal, allow_model=allow_model)
        self._store(source.id, signal, result, row=cached)
        return result

    def cached(self, source_id: str) -> TriageResult | None:
        """The stored label without computing anything. For read paths like the API."""
        row = self.session.get(SourceTriage, source_id)
        return _row_to_result(row) if row is not None else None

    # ----------------------------------------------------------------- internal

    def _store(
        self,
        source_id: str,
        signal: SourceSignal,
        result: TriageResult,
        *,
        row: SourceTriage | None,
    ) -> None:
        if row is None:
            row = SourceTriage(source_id=source_id)
            self.session.add(row)
        row.content_type = str(result.content_type)
        row.confidence = result.confidence
        row.method = str(result.method)
        row.model_name = result.model_name
        # Runners-up ride along in the same JSON column rather than earning their own:
        # they exist to explain an ambiguous call in the UI, and a column per explanatory
        # field would grow the schema for something no query filters on.
        row.cues_json = {"cues": list(result.cues), "runners_up": list(result.runners_up)}
        row.signal_fingerprint = signal.fingerprint
        row.computed_at_ms = now_ms()
        self.session.flush()


def _row_to_result(row: SourceTriage) -> TriageResult:
    payload = row.cues_json or {}
    try:
        content_type = ContentType(row.content_type)
    except ValueError:
        # A label written by a newer version, or by hand. Treated as unknown rather than
        # crashing a read path over a routing hint.
        content_type = ContentType.UNKNOWN
    try:
        method = TriageMethod(row.method)
    except ValueError:
        method = TriageMethod.NONE
    return TriageResult(
        content_type=content_type,
        confidence=row.confidence,
        method=method,
        cues=tuple(str(c) for c in payload.get("cues", [])),
        model_name=row.model_name,
        runners_up=tuple(str(c) for c in payload.get("runners_up", [])),
    )


__all__ = ["TriageService"]
