"""Resurface: bring back the things the user said they wanted to act on.

The point of collecting a video about a restaurant is to eventually go there. Saved
intentions are the one piece of knowledge in this system the user authored themselves, and
without a surface that lists them the system remembers everything except what the user
actually wanted.

Three constraints shape this module.

**It reads user state, not creator claims.** `EntityUserState.state` is the user's own
word -- "I want to go here" -- and is never mixed into the claim set. A creator saying a
restaurant is good and the user saying they want to go are different kinds of statement
with different provenance, and `PROCESSING_POLICY` treats collapsing them as a bug
(KM-003). So a card's *existence* comes from user state alone; policy and currency never
delete an intention the user recorded.

**Its support must be eligible knowledge.** What policy and currency do control is the
supporting material shown on the card. A card backed only by an excluded or
`metadata_only` source shows no support at all, because presenting that source's claims
would reintroduce the leak `knowledge.eligibility` exists to close. Support therefore goes
through the same shared rule as every other normal knowledge surface, which also means an
entity supported by three sources, one of them excluded, shows the other two.

**It ranks nothing.** No recommendation model, no scoring, no notifications. Ordering is
deterministic -- most recently acted on first, then entity name -- because a saved
intention list the user cannot predict the order of is a list they stop trusting. Anything
cleverer is a separate decision made against real usage, not a guess made now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, Entity
from douyin_knowledge.db.models.userstate import EntityUserState
from douyin_knowledge.db.models.wiki import WikiPage
from douyin_knowledge.knowledge.eligibility import eligible_claims

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "INTENT_STATES",
    "ResurfaceCard",
    "ResurfaceSupport",
    "clear_state",
    "list_cards",
    "set_state",
    "state_for",
]

#: States that mean "I intend to do something about this". Resurface lists exactly these.
#:
#: A short closed set on purpose: free text would make the list unsortable and unfilterable,
#: and the note field already exists for anything that does not fit. `visited`/`done` are
#: deliberately absent -- finishing something is a different feature (a history), and adding
#: it here would make the list a mix of "to do" and "did", which is the failure mode that
#: makes saved-item lists useless.
INTENT_STATES: tuple[str, ...] = ("want_to_go", "want_to_try", "want_to_learn")

STATUS_ACTIVE = "active"


@dataclass(frozen=True)
class ResurfaceSupport:
    """One eligible claim, with the source that said it, behind a card."""

    claim_id: str
    predicate: str
    value_text: str | None
    value_number: float | None
    unit: str | None
    currency: str | None
    source_id: str
    source_title: str | None
    source_url: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "predicate": self.predicate,
            "value_text": self.value_text,
            "value_number": self.value_number,
            "unit": self.unit,
            "currency": self.currency,
            "source_id": self.source_id,
            "source_title": self.source_title,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class ResurfaceCard:
    """A saved intention, plus whatever current knowledge still supports it."""

    entity_id: str
    entity_type: str
    canonical_name: str
    state: str
    note: str | None
    rating: float | None
    first_action_at_ms: int | None
    last_action_at_ms: int | None
    wiki_page_id: str | None
    supports: list[ResurfaceSupport] = field(default_factory=list)

    @property
    def has_eligible_support(self) -> bool:
        """Whether any current, policy-eligible source still backs this card.

        False is a real state, not an error: the user's intention stands on its own, and the
        honest presentation is "you saved this, and nothing in your collection currently
        says anything about it" rather than hiding the card or inventing support.
        """
        return bool(self.supports)

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "canonical_name": self.canonical_name,
            "state": self.state,
            "note": self.note,
            "rating": self.rating,
            "first_action_at_ms": self.first_action_at_ms,
            "last_action_at_ms": self.last_action_at_ms,
            "wiki_page_id": self.wiki_page_id,
            "has_eligible_support": self.has_eligible_support,
            "supports": [s.as_dict() for s in self.supports],
        }


def state_for(session: Session, entity_id: str) -> EntityUserState | None:
    """The user's saved state for one entity, or None."""
    return session.get(EntityUserState, entity_id)


def set_state(
    session: Session,
    entity_id: str,
    *,
    state: str,
    note: str | None = None,
    rating: float | None = None,
) -> EntityUserState:
    """Record or update the user's intention for an entity.

    `first_action_at_ms` is written once and never moved: it answers "how long has this been
    on my list", which is the question that makes a stale intention visible. Changing
    `want_to_go` to `want_to_try` updates `last_action_at_ms` only.
    """
    if state not in INTENT_STATES:
        raise ValueError(f"unknown state {state!r}; expected one of {list(INTENT_STATES)}")
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise LookupError(f"entity {entity_id} not found")

    ts = now_ms()
    row = session.get(EntityUserState, entity_id)
    if row is None:
        row = EntityUserState(entity_id=entity_id, first_action_at_ms=ts)
        session.add(row)
    row.state = state
    if note is not None:
        row.note = note or None
    if rating is not None:
        row.rating = rating
    if row.first_action_at_ms is None:
        row.first_action_at_ms = ts
    row.last_action_at_ms = ts
    row.updated_at_ms = ts
    session.flush()
    return row


def clear_state(session: Session, entity_id: str) -> bool:
    """Drop the intention, keeping the note and rating.

    The row survives with `state = None` rather than being deleted: a user who clears
    "want to go" has not necessarily withdrawn the note they wrote about why, and losing it
    silently would be the kind of quiet data loss this system is built to avoid.
    """
    row = session.get(EntityUserState, entity_id)
    if row is None or row.state is None:
        return False
    row.state = None
    row.last_action_at_ms = now_ms()
    row.updated_at_ms = row.last_action_at_ms
    session.flush()
    return True


def list_cards(
    session: Session,
    *,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    supports_per_card: int = 3,
) -> tuple[list[ResurfaceCard], int]:
    """Saved intentions, newest action first, with eligible support attached.

    Returns `(cards, total)` so a caller can page without recounting. Support is fetched for
    the returned page only -- the whole point of the limit is to avoid loading every claim in
    the corpus to render ten cards.
    """
    stmt = (
        select(EntityUserState, Entity)
        .join(Entity, Entity.id == EntityUserState.entity_id)
        .where(
            EntityUserState.state.in_(INTENT_STATES),
            # Merged entities are redirects; showing both sides would list one restaurant
            # twice under two names.
            Entity.status == STATUS_ACTIVE,
            Entity.merged_into_entity_id.is_(None),
        )
    )
    if state is not None:
        stmt = stmt.where(EntityUserState.state == state)

    rows = list(session.execute(stmt))
    total = len(rows)

    # Deterministic and stable: an intention with no recorded action time sorts last rather
    # than first, because `None` there means "written before this field existed", not "just
    # now". Name is the tiebreaker so two states saved in the same millisecond do not swap
    # places between reads.
    rows.sort(key=lambda r: (-(r[0].last_action_at_ms or 0), r[1].canonical_name))
    page = rows[offset : offset + limit]
    if not page:
        return [], total

    entity_ids = [entity.id for _, entity in page]
    supports = _supports_for(session, entity_ids, per_entity=supports_per_card)
    pages = {
        wiki.entity_id: wiki.id
        for wiki in session.scalars(
            select(WikiPage).where(
                WikiPage.entity_id.in_(entity_ids), WikiPage.status == STATUS_ACTIVE
            )
        )
        if wiki.entity_id
    }

    cards = [
        ResurfaceCard(
            entity_id=entity.id,
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            state=str(user_state.state),
            note=user_state.note,
            rating=user_state.rating,
            first_action_at_ms=user_state.first_action_at_ms,
            last_action_at_ms=user_state.last_action_at_ms,
            wiki_page_id=pages.get(entity.id),
            supports=supports.get(entity.id, []),
        )
        for user_state, entity in page
    ]
    return cards, total


def _supports_for(
    session: Session, entity_ids: list[str], *, per_entity: int
) -> dict[str, list[ResurfaceSupport]]:
    """Eligible claims about each entity, capped per entity.

    Goes through `eligible_claims`, so a source that is locally deleted, excluded,
    `metadata_only`, or pointing at a superseded run contributes nothing, and a claim whose
    grounding was downgraded is not presented as fact. That is the whole reason this helper
    exists rather than a plain join: the card is a knowledge surface, and the rule for what
    normal knowledge surfaces may show lives in one place.
    """
    if not entity_ids:
        return {}

    claims = eligible_claims(
        session,
        select(Claim)
        .where(Claim.subject_entity_id.in_(entity_ids))
        .order_by(Claim.created_at_ms),
    )
    if not claims:
        return {}

    source_ids = sorted({c.source_id for c in claims})
    sources = {
        s.id: s for s in session.scalars(select(Source).where(Source.id.in_(source_ids)))
    }

    out: dict[str, list[ResurfaceSupport]] = {}
    for claim in claims:
        entity_id = claim.subject_entity_id
        if not entity_id:
            continue
        bucket = out.setdefault(entity_id, [])
        if len(bucket) >= per_entity:
            continue
        source = sources.get(claim.source_id)
        bucket.append(
            ResurfaceSupport(
                claim_id=claim.id,
                predicate=claim.predicate,
                value_text=claim.value_text,
                value_number=claim.value_number,
                unit=claim.unit,
                currency=claim.currency,
                source_id=claim.source_id,
                source_title=(source.title or source.caption_raw) if source else None,
                source_url=source.source_url if source else None,
            )
        )
    return out
