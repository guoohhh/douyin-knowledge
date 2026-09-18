"""Resolve entity mentions to canonical entities.

The governing rule is ENT-002: **normalized identity is the only signal trusted
for an automatic merge.** Fuzzy similarity is computed, but it can only ever
propose — never commit — a link.

The reason is asymmetric cost. A missed merge leaves two entity pages that a
user can join later with one click, and the underlying claims stay correct. A
wrong merge silently fuses two different restaurants into one page, and every
claim attached to it becomes untrue in a way no one can see from the UI: the
provenance chain still looks intact, it just points at the wrong entity. Edit
distance cannot tell "好运茶餐厅" from "好运茶餐店" (one character, plausibly a
typo, plausibly a different shop), so it does not get to decide.

Resolution outcomes, written to ``EntityMention.resolution_status``:

* ``resolved``   — normalized identity or a registered alias matched exactly.
* ``ambiguous``  — one or more fuzzy candidates found; needs human confirmation.
                   ``resolved_entity_id`` stays NULL.
* ``new``        — nothing matched; a fresh canonical entity was created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from douyin_knowledge.core.text import normalize_identity
from douyin_knowledge.db.models.entities import Entity, EntityAlias, EntityMention
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

logger = get_logger(__name__)

STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NEW = "new"
STATUS_UNRESOLVED = "unresolved"

# Fuzzy matches at or above this score are worth showing a human. They are
# never auto-applied, so this threshold trades reviewer noise against recall,
# not correctness.
REVIEW_THRESHOLD = 0.72


@dataclass(frozen=True)
class ResolutionCandidate:
    entity_id: str
    canonical_name: str
    score: float
    reason: str


@dataclass
class ResolutionOutcome:
    """What resolution decided, and why — surfaced in the review queue."""

    mention_id: str
    status: str
    entity_id: str | None
    candidates: list[ResolutionCandidate]
    reason: str

    @property
    def needs_review(self) -> bool:
        return self.status == STATUS_AMBIGUOUS


class EntityResolver:
    """Deterministic-merge resolver with a human-review path for the rest."""

    def __init__(self, *, review_threshold: float = REVIEW_THRESHOLD) -> None:
        self.review_threshold = review_threshold

    # ------------------------------------------------------------------ public

    def resolve_mention(
        self, session: Session, mention: EntityMention, *, create_if_missing: bool = True
    ) -> ResolutionOutcome:
        """Resolve one mention. Never merges on fuzzy evidence alone."""
        if mention.resolved_entity_id:
            return ResolutionOutcome(
                mention.id, mention.resolution_status or STATUS_RESOLVED,
                mention.resolved_entity_id, [], "already_resolved"
            )

        normalized = mention.normalized_text or normalize_identity(mention.mention_text)
        if not normalized:
            mention.resolution_status = STATUS_UNRESOLVED
            return ResolutionOutcome(mention.id, STATUS_UNRESOLVED, None, [], "empty_mention")

        exact = self._exact_match(session, normalized, mention.entity_type_hint)
        if exact is not None:
            mention.resolved_entity_id = exact.id
            mention.resolution_status = STATUS_RESOLVED
            mention.resolution_confidence = 1.0
            self._ensure_alias(session, exact, mention.mention_text, normalized)
            return ResolutionOutcome(
                mention.id, STATUS_RESOLVED, exact.id, [], "exact_normalized_identity"
            )

        candidates = self._fuzzy_candidates(session, mention, normalized)
        if candidates:
            # Deliberately does NOT set resolved_entity_id (ENT-002).
            mention.resolution_status = STATUS_AMBIGUOUS
            mention.resolution_confidence = candidates[0].score
            context = dict(mention.context_json or {})
            context["resolution_candidates"] = [
                {"entity_id": c.entity_id, "name": c.canonical_name,
                 "score": round(c.score, 4), "reason": c.reason}
                for c in candidates[:5]
            ]
            mention.context_json = context
            logger.info(
                "mention_needs_review",
                extra={"mention": mention.mention_text, "candidates": len(candidates)},
            )
            return ResolutionOutcome(
                mention.id, STATUS_AMBIGUOUS, None, candidates, "fuzzy_candidates_need_review"
            )

        if not create_if_missing:
            mention.resolution_status = STATUS_UNRESOLVED
            return ResolutionOutcome(mention.id, STATUS_UNRESOLVED, None, [], "no_match")

        entity = self._create_entity(session, mention, normalized)
        mention.resolved_entity_id = entity.id
        mention.resolution_status = STATUS_NEW
        mention.resolution_confidence = 1.0
        return ResolutionOutcome(mention.id, STATUS_NEW, entity.id, [], "created_new_entity")

    def resolve_batch(
        self, session: Session, mentions: list[EntityMention]
    ) -> list[ResolutionOutcome]:
        """Resolve mentions in order.

        Order matters: the first mention of a name creates the entity, and later
        mentions in the same batch then match it exactly. Flushing per mention
        keeps that visible to subsequent lookups.
        """
        outcomes: list[ResolutionOutcome] = []
        for mention in mentions:
            outcome = self.resolve_mention(session, mention)
            session.flush()
            outcomes.append(outcome)
        return outcomes

    def confirm_resolution(
        self, session: Session, mention: EntityMention, entity_id: str
    ) -> ResolutionOutcome:
        """Apply a human decision from the review queue.

        This is the *only* path by which a fuzzy match becomes a real link.
        """
        entity = session.get(Entity, entity_id)
        if entity is None:
            raise ValueError(f"unknown entity_id: {entity_id}")
        target = self._follow_merge(session, entity)
        mention.resolved_entity_id = target.id
        mention.resolution_status = STATUS_RESOLVED
        mention.resolution_confidence = 1.0
        self._ensure_alias(
            session, target, mention.mention_text,
            mention.normalized_text or normalize_identity(mention.mention_text),
        )
        return ResolutionOutcome(mention.id, STATUS_RESOLVED, target.id, [], "human_confirmed")

    # ----------------------------------------------------------------- matching

    def _exact_match(
        self, session: Session, normalized: str, entity_type: str | None
    ) -> Entity | None:
        """Match on normalized canonical name, then on registered aliases."""
        stmt = select(Entity).where(
            Entity.normalized_name == normalized,
            Entity.merged_into_entity_id.is_(None),
        )
        if entity_type:
            stmt = stmt.where(Entity.entity_type == entity_type)
        entity = session.scalars(stmt).first()
        if entity is not None:
            return entity

        alias_stmt = (
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(
                EntityAlias.normalized_alias == normalized,
                Entity.merged_into_entity_id.is_(None),
            )
        )
        if entity_type:
            alias_stmt = alias_stmt.where(Entity.entity_type == entity_type)
        return session.scalars(alias_stmt).first()

    def _fuzzy_candidates(
        self, session: Session, mention: EntityMention, normalized: str
    ) -> list[ResolutionCandidate]:
        stmt = select(Entity).where(Entity.merged_into_entity_id.is_(None))
        if mention.entity_type_hint:
            stmt = stmt.where(Entity.entity_type == mention.entity_type_hint)

        candidates: list[ResolutionCandidate] = []
        for entity in session.scalars(stmt):
            score, reason = self._similarity(normalized, entity.normalized_name)
            if score >= self.review_threshold:
                candidates.append(
                    ResolutionCandidate(entity.id, entity.canonical_name, score, reason)
                )
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _similarity(self, left: str, right: str) -> tuple[float, str]:
        if not left or not right:
            return (0.0, "empty")
        if left == right:
            return (1.0, "identical_normalized")

        if left in right or right in left:
            score = min(len(left), len(right)) / max(len(left), len(right))
            return (score * 0.95, "substring")

        distance = self._levenshtein(left, right)
        longest = max(len(left), len(right))
        return (max(0.0, 1.0 - distance / longest) * 0.9, f"edit_distance_{distance}")

    @staticmethod
    def _levenshtein(left: str, right: str) -> int:
        if len(left) < len(right):
            left, right = right, left
        if not right:
            return len(left)

        previous = list(range(len(right) + 1))
        for i, lchar in enumerate(left):
            current = [i + 1]
            for j, rchar in enumerate(right):
                current.append(
                    min(previous[j + 1] + 1, current[j] + 1, previous[j] + (lchar != rchar))
                )
            previous = current
        return previous[-1]

    # ------------------------------------------------------------------ writes

    def _create_entity(
        self, session: Session, mention: EntityMention, normalized: str
    ) -> Entity:
        entity = Entity(
            entity_type=mention.entity_type_hint or "unknown",
            canonical_name=mention.mention_text.strip(),
            normalized_name=normalized,
            status="active",
        )
        session.add(entity)
        session.flush()
        logger.info(
            "entity_created",
            extra={"entity_id": entity.id, "name": entity.canonical_name},
        )
        return entity

    def _ensure_alias(
        self, session: Session, entity: Entity, alias: str, normalized: str
    ) -> None:
        """Register a surface form as an alias so later mentions match exactly."""
        alias = alias.strip()
        if not alias or not normalized or normalized == entity.normalized_name:
            return

        existing = session.scalars(
            select(EntityAlias).where(
                EntityAlias.entity_id == entity.id,
                EntityAlias.normalized_alias == normalized,
            )
        ).first()
        if existing is not None:
            return

        session.add(
            EntityAlias(
                entity_id=entity.id,
                alias=alias,
                normalized_alias=normalized,
                origin="resolver",
            )
        )

    @staticmethod
    def _follow_merge(session: Session, entity: Entity, *, max_hops: int = 8) -> Entity:
        """Follow ``merged_into_entity_id`` to the surviving entity.

        Bounded to stay safe against a cycle introduced by a bad merge.
        """
        current = entity
        for _ in range(max_hops):
            if current.merged_into_entity_id is None:
                return current
            nxt = session.get(Entity, current.merged_into_entity_id)
            if nxt is None:
                return current
            current = nxt
        logger.warning("merge_chain_too_long", extra={"entity_id": entity.id})
        return current


__all__ = [
    "STATUS_AMBIGUOUS",
    "STATUS_NEW",
    "STATUS_RESOLVED",
    "STATUS_UNRESOLVED",
    "EntityResolver",
    "ResolutionCandidate",
    "ResolutionOutcome",
]
