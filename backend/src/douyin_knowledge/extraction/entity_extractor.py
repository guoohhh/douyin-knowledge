"""Extract entity mentions from evidence units.

A mention is a *span of observed text*, not a fact about the world. It records
that this source, in this processing run, said this string — which is why every
mention is linked back to the evidence it came from through
``EntityMentionEvidence``. Resolution to a canonical entity is a separate,
reversible decision made by `EntityResolver` (ENT-002).

Extraction is stateless with respect to the entity table: it never looks up or
creates entities. That separation is what makes reprocessing safe — a new run
produces new mentions, and only resolution touches shared canonical state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from douyin_knowledge.core.text import normalize_identity, truncate
from douyin_knowledge.db.models.entities import EntityMention, EntityMentionEvidence
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from douyin_knowledge.ai.providers import StructuredModel
    from douyin_knowledge.db.models.processing import EvidenceUnit

logger = get_logger(__name__)

MENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "entity_mentions",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Exact surface form as it appears"},
                    "entity_type": {
                        "type": "string",
                        "enum": ["place", "dish", "person", "brand", "tool", "topic", "unknown"],
                    },
                    "context": {"type": "string", "description": "Surrounding text"},
                },
                "required": ["text", "entity_type"],
            },
        }
    },
    "required": ["mentions"],
}

_PROMPT = """你是一个信息抽取器。从下面的短视频文本中抽取出被提到的实体。

规则:
- 只抽取文本中**字面出现**的名称,不要推断或补全。
- entity_type 从这几类里选: place(地点/店铺) dish(菜品/饮品) person(人物) brand(品牌) tool(工具/软件) topic(话题) unknown。
- 同一个实体多次出现只返回一次,用最完整的那个写法。
- 如果没有实体,返回空数组。

<<<TEXT>>>
{text}"""


@dataclass
class ExtractedMention:
    """One mention before it is persisted."""

    text: str
    entity_type: str
    context: str
    evidence_id: str


class EntityExtractor:
    """Turns evidence text into `EntityMention` rows."""

    def __init__(self, structured_model: StructuredModel) -> None:
        self.model = structured_model

    def extract_from_evidence(
        self,
        session: Session,
        evidence_units: Sequence[EvidenceUnit],
        *,
        source_id: str,
        processing_run_id: str,
    ) -> list[EntityMention]:
        """Extract and persist mentions for a run's evidence.

        Mentions are deduplicated by normalized identity *across* the whole run:
        a restaurant named in both the caption and the subtitle is one mention
        carrying two evidence links, not two competing mentions.
        """
        candidates: list[ExtractedMention] = []
        for unit in evidence_units:
            text = unit.normalized_text or unit.raw_text
            if not text or not text.strip():
                continue
            candidates.extend(self._extract_one(unit, text))

        if not candidates:
            return []

        # normalized identity -> (mention row, evidence ids)
        grouped: dict[str, tuple[EntityMention, list[str]]] = {}
        for candidate in candidates:
            normalized = normalize_identity(candidate.text)
            if not normalized:
                continue
            key = f"{candidate.entity_type}:{normalized}"
            existing = grouped.get(key)
            if existing is not None:
                if candidate.evidence_id not in existing[1]:
                    existing[1].append(candidate.evidence_id)
                # Keep the longest surface form as the canonical spelling.
                if len(candidate.text) > len(existing[0].mention_text):
                    existing[0].mention_text = candidate.text
                continue

            mention = EntityMention(
                source_id=source_id,
                processing_run_id=processing_run_id,
                mention_text=candidate.text,
                normalized_text=normalized,
                entity_type_hint=candidate.entity_type,
                context_json={"context": truncate(candidate.context, 300)},
                resolution_status="unresolved",
            )
            grouped[key] = (mention, [candidate.evidence_id])

        mentions: list[EntityMention] = []
        for mention, evidence_ids in grouped.values():
            session.add(mention)
            session.flush()  # need mention.id for the join rows
            for evidence_id in evidence_ids:
                session.add(
                    EntityMentionEvidence(
                        entity_mention_id=mention.id, evidence_id=evidence_id
                    )
                )
            mentions.append(mention)

        logger.info(
            "mentions_extracted",
            extra={
                "source_id": source_id,
                "run_id": processing_run_id,
                "count": len(mentions),
            },
        )
        return mentions

    def _extract_one(self, unit: EvidenceUnit, text: str) -> list[ExtractedMention]:
        try:
            response = self.model.extract(_PROMPT.format(text=text), MENTION_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            # One unparseable unit must not abandon the rest of the source.
            logger.warning(
                "mention_extraction_failed",
                extra={"evidence_id": unit.id, "error": str(exc)},
            )
            return []

        raw = response.data.get("mentions") if isinstance(response.data, dict) else None
        if not isinstance(raw, list):
            return []

        results: list[ExtractedMention] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            surface = str(item.get("text") or "").strip()
            if not surface:
                continue
            # A model can hallucinate a name that is not in the text; a mention
            # that is not literally present is not a mention.
            if surface not in text:
                logger.debug(
                    "mention_not_in_text", extra={"mention": surface, "evidence_id": unit.id}
                )
                continue
            results.append(
                ExtractedMention(
                    text=surface,
                    entity_type=str(item.get("entity_type") or "unknown"),
                    context=str(item.get("context") or text),
                    evidence_id=unit.id,
                )
            )
        return results


__all__ = ["EntityExtractor", "ExtractedMention", "MENTION_SCHEMA"]
