"""Extract claims from evidence units.

A Claim is an *atomic, source-attributed assertion* — never a global fact
(KM-003). "好运茶餐厅人均八十" is not stored as the price of that restaurant; it
is stored as "this creator, in this video, said the price was 80". That framing
is what lets two videos disagree without either being wrong, and it is why
`attribution` and `provenance_type` are non-negotiable fields rather than
metadata.

Two consequences shape this module:

* Every claim gets `ClaimEvidence` rows. A claim with no evidence cannot be
  cited, so it is not written at all.
* Subject linking prefers a resolved entity but always keeps `subject_text`.
  Resolution can be revised later; the words the creator used cannot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from douyin_knowledge.core.text import normalize_identity, truncate
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import StructuredModel
    from douyin_knowledge.db.models.entities import EntityMention
    from douyin_knowledge.db.models.processing import EvidenceUnit

logger = get_logger(__name__)

VALUE_TYPES = ("text", "number", "boolean", "json", "duration", "date")
CLAIM_KINDS = ("attribute", "measurement", "evaluation", "relation", "event")
PROVENANCE_TYPES = (
    "creator_statement",  # the creator asserts it directly
    "creator_opinion",  # hedged or clearly subjective
    "on_screen_text",  # OCR'd from the video itself
    "third_party",  # the creator quotes someone else
)

CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "claims",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject_text": {"type": "string"},
                    "predicate": {"type": "string"},
                    "value_type": {"type": "string", "enum": list(VALUE_TYPES)},
                    "value_text": {"type": "string"},
                    "value_number": {"type": "number"},
                    "unit": {"type": "string"},
                    "currency": {"type": "string"},
                    "claim_kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                    "provenance_type": {"type": "string", "enum": list(PROVENANCE_TYPES)},
                    "confidence": {"type": "number"},
                    "evidence_span": {
                        "type": "string",
                        "description": "The exact sentence supporting this claim",
                    },
                },
                "required": ["predicate", "value_type", "claim_kind"],
            },
        }
    },
    "required": ["claims"],
}

_PROMPT = """你从短视频文本中抽取结构化断言(claim)。

重要原则:
- 每条 claim 必须**只来自文本本身**,不要用你的世界知识补充。
- claim 是"创作者说了什么",不是"事实是什么"。
- 主观表达(我觉得/可能/应该)用 provenance_type=creator_opinion,客观陈述用 creator_statement。
- 价格类用 value_type=number 并填 value_number 和 currency。
- predicate 用简短英文 snake_case,例如 price_per_person / signature_item / near / closing_hour / recommended。
- evidence_span 必须是原文中的完整句子。
- 没有可抽取的断言就返回空数组。

<<<TEXT>>>
{text}"""


class ClaimExtractor:
    """Turns evidence text into `Claim` + `ClaimEvidence` rows."""

    def __init__(self, structured_model: StructuredModel) -> None:
        self.model = structured_model

    def extract_from_evidence(
        self,
        session: Session,
        evidence_units: Sequence[EvidenceUnit],
        *,
        source_id: str,
        processing_run_id: str,
        mentions: Sequence[EntityMention] = (),
        creator_name: str | None = None,
    ) -> list[Claim]:
        """Extract and persist claims for one processing run."""
        # normalized subject text -> resolved entity id, for subject linking.
        subject_index: dict[str, str] = {}
        for mention in mentions:
            if mention.resolved_entity_id and mention.normalized_text:
                subject_index.setdefault(mention.normalized_text, mention.resolved_entity_id)

        attribution = creator_name or "unknown_creator"
        claims: list[Claim] = []

        for unit in evidence_units:
            text = unit.normalized_text or unit.raw_text
            if not text or not text.strip():
                continue

            for payload in self._extract_one(unit, text):
                claim = self._build_claim(
                    payload,
                    source_id=source_id,
                    processing_run_id=processing_run_id,
                    subject_index=subject_index,
                    attribution=attribution,
                )
                if claim is None:
                    continue
                session.add(claim)
                session.flush()  # need claim.id for the evidence link
                session.add(
                    ClaimEvidence(
                        claim_id=claim.id,
                        evidence_id=unit.id,
                        support_role="supports",
                    )
                )
                claims.append(claim)

        logger.info(
            "claims_extracted",
            extra={"source_id": source_id, "run_id": processing_run_id, "count": len(claims)},
        )
        return claims

    def _extract_one(self, unit: EvidenceUnit, text: str) -> list[dict[str, Any]]:
        try:
            response = self.model.extract(_PROMPT.format(text=text), CLAIM_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "claim_extraction_failed", extra={"evidence_id": unit.id, "error": str(exc)}
            )
            return []

        raw = response.data.get("claims") if isinstance(response.data, dict) else None
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _build_claim(
        self,
        payload: dict[str, Any],
        *,
        source_id: str,
        processing_run_id: str,
        subject_index: dict[str, str],
        attribution: str,
    ) -> Claim | None:
        predicate = str(payload.get("predicate") or "").strip()
        if not predicate:
            return None

        value_type = str(payload.get("value_type") or "text")
        if value_type not in VALUE_TYPES:
            value_type = "text"
        claim_kind = str(payload.get("claim_kind") or "attribute")
        if claim_kind not in CLAIM_KINDS:
            claim_kind = "attribute"
        provenance_type = str(payload.get("provenance_type") or "creator_statement")
        if provenance_type not in PROVENANCE_TYPES:
            provenance_type = "creator_statement"

        subject_text = payload.get("subject_text")
        subject_text = str(subject_text).strip() if subject_text else None
        subject_entity_id = (
            subject_index.get(normalize_identity(subject_text)) if subject_text else None
        )

        # The CHECK constraint requires some subject. A claim whose subject we
        # cannot name at all is not interpretable later, so it is dropped rather
        # than stored against a placeholder.
        if not subject_text and not subject_entity_id:
            logger.debug("claim_without_subject", extra={"predicate": predicate})
            return None

        value_number = payload.get("value_number")
        try:
            value_number = float(value_number) if value_number is not None else None
        except (TypeError, ValueError):
            value_number = None

        confidence = payload.get("confidence")
        try:
            confidence = min(1.0, max(0.0, float(confidence))) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        value_json = payload.get("value_json")
        span = payload.get("evidence_span")
        if span:
            value_json = dict(value_json) if isinstance(value_json, dict) else {}
            value_json["evidence_span"] = truncate(str(span), 500)

        value_text = payload.get("value_text")
        return Claim(
            source_id=source_id,
            processing_run_id=processing_run_id,
            subject_entity_id=subject_entity_id,
            subject_text=subject_text,
            predicate=predicate,
            value_type=value_type,
            value_text=str(value_text) if value_text is not None else None,
            value_number=value_number,
            value_json=value_json if isinstance(value_json, dict) else None,
            unit=str(payload["unit"]) if payload.get("unit") else None,
            currency=str(payload["currency"]) if payload.get("currency") else None,
            claim_kind=claim_kind,
            provenance_type=provenance_type,
            attribution=attribution,
            confidence=confidence,
        )


__all__ = ["CLAIM_SCHEMA", "ClaimExtractor"]
