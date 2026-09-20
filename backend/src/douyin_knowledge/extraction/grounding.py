"""Hard grounding validation for model-produced claims (P1-2).

A claim is not durable merely because it names an EvidenceUnit. This module validates:
- evidence belongs to the correct source/run context
- model-returned span occurs in the evidence after normalization
- literal/numeric values are supported by the text
- invalid grounding is rejected or downgraded with recorded reasons

GPT's validator was `quote in evidence.text and value in quote`, which works for English but
is brittle for CJK: whitespace, full-width characters, and ASR punctuation vary. This
normalizes before matching and handles Chinese numerals (八十 → 80).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from douyin_knowledge.core.text import normalize_ws

if TYPE_CHECKING:  # pragma: no cover - typing only
    from douyin_knowledge.db.models.processing import EvidenceUnit


@dataclass(frozen=True)
class GroundingVerdict:
    """Result of validating one claim's grounding."""

    passed: bool
    status: str  # "valid" | "rejected_context" | "rejected_span" | "rejected_value" | "downgraded"
    reason: str | None = None
    #: Where the supporting span was found (char offset in normalized evidence), for audit.
    span_offset: int | None = None
    #: Confidence cap applied when downgrading. None if not downgraded.
    confidence_cap: float | None = None


#: Grounding statuses whose claims may be used to *assert* something to the user.
#:
#: `None` is in here on purpose: claims written before migration 0005 have no verdict, and
#: treating "not yet validated" as "failed validation" would silently empty the wiki of
#: every pre-existing claim on upgrade. They are grandfathered until reprocessed.
_ASSERTABLE = frozenset({None, "valid"})


def is_assertable(grounding_status: str | None) -> bool:
    """Whether a stored claim may be used to state a fact in derived output.

    A `downgraded` claim stays in SQLite -- it is the audit trail for what the model
    produced, and the run counters are built from it -- but it must not reach the wiki or
    a cited answer. The alternative was capping its confidence, which is what the first
    version did, and that turned out to be decorative: nothing in the wiki composer or the
    answer generator reads `confidence`, so a claim with an invented supporting span
    rendered exactly like a verified one.

    Shared by the wiki builder and the retriever rather than duplicated, so the two cannot
    drift into disagreeing about which claims are real.
    """
    return grounding_status in _ASSERTABLE


#: Chinese numeral mapping for common ranges (一 through 万).
_CHINESE_NUMERALS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100, "千": 1000, "万": 10000,
}


def _parse_chinese_number(text: str) -> float | None:
    """Parse common Chinese numerals: 八十 → 80, 一百二十 → 120.

    Does not handle complex compositions (三千五百万) or literary forms. Returns None if the
    text cannot be parsed rather than guessing, so a mismatch is a real mismatch.
    """
    text = text.strip()
    if not text or not any(ch in _CHINESE_NUMERALS for ch in text):
        return None

    # 八十 / 九十五
    if len(text) == 2 and text[1] == "十":
        tens = _CHINESE_NUMERALS.get(text[0])
        return float(tens * 10) if tens is not None else None
    if len(text) == 3 and text[1] == "十":
        tens = _CHINESE_NUMERALS.get(text[0])
        ones = _CHINESE_NUMERALS.get(text[2])
        if tens is not None and ones is not None:
            return float(tens * 10 + ones)

    # 一百 / 一百二 / 一百二十
    if "百" in text:
        parts = text.split("百", 1)
        hundreds = _CHINESE_NUMERALS.get(parts[0], 1) if parts[0] else 1
        remainder = parts[1] if len(parts) > 1 else ""
        if not remainder:
            return float(hundreds * 100)
        if len(remainder) == 1:
            # 一百二 = 120
            tens = _CHINESE_NUMERALS.get(remainder)
            return float(hundreds * 100 + (tens or 0) * 10) if tens is not None else None
        if len(remainder) == 2 and remainder[0] == "十":
            ones = _CHINESE_NUMERALS.get(remainder[1])
            return float(hundreds * 100 + 10 + (ones or 0)) if ones is not None else None
        if len(remainder) == 3 and remainder[1] == "十":
            tens = _CHINESE_NUMERALS.get(remainder[0])
            ones = _CHINESE_NUMERALS.get(remainder[2])
            if tens is not None and ones is not None:
                return float(hundreds * 100 + tens * 10 + ones)

    return None


def _normalize_for_grounding(text: str) -> str:
    """Aggressive normalization for span matching.

    NFKC folds full-width to half-width, punctuation is dropped and whitespace collapsed.
    This is deliberately looser than an exact-substring test: a transcript writes "人均八十"
    and the model quotes "人均八十，" -- treating that as a hallucination would reject correct
    extractions, which is the expensive kind of wrong here.
    """
    normalized = unicodedata.normalize("NFKC", text)
    # Strip common noise: 、。，！？ plus EN/CN mixed punctuation
    normalized = re.sub(r"[、。，！？,.!?]+", "", normalized)
    return normalize_ws(normalized).casefold()


def _find_value_in_text(text: str, value_text: str | None, value_number: float | None) -> bool:
    """Check if a literal/numeric value is supported by the text.

    For numbers, also tries Chinese numeral parsing (八十 → 80). A number must match
    exactly or within 1% (handles rounding like 80 vs 80.0).
    """
    normed = _normalize_for_grounding(text)

    if value_text:
        if _normalize_for_grounding(value_text) in normed:
            return True

    if value_number is not None:
        # Try Arabic digits
        if str(int(value_number)) in normed or f"{value_number:.1f}" in normed:
            return True
        # Try Chinese numerals in the text
        for match in re.finditer(r"[零一二三四五六七八九十百千万]+", text):
            parsed = _parse_chinese_number(match.group())
            if parsed is not None and abs(parsed - value_number) < max(1.0, value_number * 0.01):
                return True

    return False


#: Only these value types are quotations that must appear in the evidence.
#:
#: The others are *derived*, and demanding they appear literally rejects correct work:
#: `boolean` stores `"true"` for 推荐 -- the English word is never in a Chinese transcript;
#: `date` and `duration` store normalized forms, so a creator saying 明天 yields a resolved
#: date that is deliberately not what the text says; `json` is a structure, not a phrase.
#: This was measured, not assumed -- the first version of this validator silently dropped
#: every `recommended` claim in the demo corpus, taking a wiki page down with it.
_LITERAL_VALUE_TYPES = frozenset({"text", "number"})


class GroundingValidator:
    """Validates claims before persistence (P1-2).

    Checks:
    1. Evidence belongs to the correct source (structural integrity).
    2. The supporting span appears in the evidence text (after normalization).
    3. Literal/numeric values are grounded (including Chinese numerals).

    Verdicts:
    - `rejected_context`: evidence.source_id != expected → hard reject, do not persist.
    - `rejected_span`: span missing → downgrade (strip span, cap confidence at 0.5).
    - `rejected_value`: value claims unsupported → hard reject.
    - `downgraded`: span found but weak match → cap confidence.
    - `valid`: all checks passed.
    """

    def validate(
        self,
        evidence: EvidenceUnit,
        *,
        expected_source_id: str,
        evidence_span: str | None,
        value_type: str = "text",
        value_text: str | None = None,
        value_number: float | None = None,
    ) -> GroundingVerdict:
        """Validate one claim's grounding against its evidence.

        Args:
            evidence: The EvidenceUnit this claim references.
            expected_source_id: The source_id the claim will be written with.
            evidence_span: The model-returned supporting span.
            value_text: The claim's value_text, if present.
            value_number: The claim's value_number, if present.

        Returns:
            A verdict with passed/status/reason. Rejected claims should not be persisted;
            downgraded claims should have confidence capped and bogus spans stripped.
        """
        # 1. Context check: evidence must belong to the right source
        if evidence.source_id != expected_source_id:
            return GroundingVerdict(
                passed=False,
                status="rejected_context",
                reason=f"evidence {evidence.id} belongs to {evidence.source_id}, not {expected_source_id}",
            )

        evidence_text = evidence.normalized_text or evidence.raw_text or ""
        if not evidence_text.strip():
            return GroundingVerdict(
                passed=False,
                status="rejected_span",
                reason="evidence text is empty",
            )

        normed_evidence = _normalize_for_grounding(evidence_text)

        # The value check runs first and independently of the span. Ordering the other way
        # round -- returning the span downgrade immediately -- meant a claim with both a
        # hallucinated span *and* an unsupported number was kept as merely "downgraded",
        # so the worse of the two defects escaped because the milder one was found first.
        if value_type in _LITERAL_VALUE_TYPES and (value_text or value_number is not None):
            if not _find_value_in_text(evidence_text, value_text, value_number):
                return GroundingVerdict(
                    passed=False,
                    status="rejected_value",
                    reason=f"value {value_text or value_number} not found in evidence",
                )

        span_offset: int | None = None
        if evidence_span and evidence_span.strip():
            normed_span = _normalize_for_grounding(evidence_span)
            if normed_span in normed_evidence:
                span_offset = normed_evidence.index(normed_span)
            else:
                # A missing span is a downgrade rather than a rejection: the assertion can
                # still be a fair reading of the evidence when the quotation is not exact,
                # and the row is worth keeping as the record of what the model produced.
                # `is_assertable` is what keeps it out of the wiki and cited answers.
                return GroundingVerdict(
                    passed=False,
                    status="downgraded",
                    reason="evidence_span not found in evidence text after normalization",
                    confidence_cap=0.5,
                )

        return GroundingVerdict(
            passed=True,
            status="valid",
            span_offset=span_offset,
        )


__all__ = ["GroundingValidator", "GroundingVerdict", "is_assertable"]
