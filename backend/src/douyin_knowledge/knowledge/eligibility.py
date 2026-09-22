"""The one definition of "current normal knowledge".

A claim may be presented as knowledge only when *all* of the following hold:

1. its source is not locally deleted;
2. its source has a current processing-run pointer (DB-004);
3. the source's current policy action is not in ``HIDDEN_ACTIONS``;
4. the claim belongs to that current run;
5. the claim is assertable, i.e. grounding validation did not downgrade it (DEC-017).

Rules 1-2 are currency, rule 3 is policy, rule 5 is grounding. Three different concerns,
one predicate, because a surface that applies two of the three is a leak. That is not
hypothetical: the Knowledge API applied only 1, 2 and 4, so making a source
``metadata_only`` removed it from Ask and search while its claims stayed on entity pages.

**Audit surfaces are deliberately excluded from this module.** Source detail, revision
history and the policy decision log must keep showing historical and downgraded material,
clearly labelled -- that is their job. They query the tables directly and should not route
through here (AGENTS 4: disappearance is not deletion).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.extraction.grounding import ASSERTABLE_STATUSES, is_assertable
from douyin_knowledge.policy.models import HIDDEN_ACTIONS

__all__ = [
    "apply_claim_eligibility",
    "current_eligible_runs",
    "eligible_claims",
    "eligible_claim_ids",
    "is_source_eligible",
]


def apply_claim_eligibility(stmt: Select[Any]) -> Select[Any]:
    """Add the five eligibility rules to a ``Claim`` select as SQL.

    The same predicate as :func:`eligible_claims`, expressed as joins instead of a
    Python comprehension. Two implementations of one rule is a drift risk, and it is
    accepted here for a specific reason: :func:`eligible_claims` loads every row its
    statement matches, which its docstring bounds at "one entity's claims, not the
    corpus". Structured retrieval starts from a *predicate* (`price_per_person` over
    every entity) and that bound does not hold, so the filter has to happen in SQLite
    where ``LIMIT`` can also apply.

    The drift risk is contained by ``test_structured_retrieval.py``, which asserts both
    functions return the same claim ids over a fixture built to exercise all five rules.
    Grounding statuses come from :data:`ASSERTABLE_STATUSES` rather than being re-listed,
    and the ``NULL`` member needs its own ``IS NULL`` term because SQL ``IN`` never
    matches ``NULL``.
    """
    statuses = [s for s in ASSERTABLE_STATUSES if s is not None]
    grounding_ok = Claim.grounding_status.in_(sorted(statuses))
    if None in ASSERTABLE_STATUSES:
        grounding_ok = or_(grounding_ok, Claim.grounding_status.is_(None))

    return (
        stmt.join(SourceProcessingState, SourceProcessingState.source_id == Claim.source_id)
        .join(Source, Source.id == Claim.source_id)
        .where(
            Source.locally_deleted_at_ms.is_(None),
            SourceProcessingState.current_processing_run_id.is_not(None),
            SourceProcessingState.current_policy_action.not_in(sorted(HIDDEN_ACTIONS)),
            SourceProcessingState.current_processing_run_id == Claim.processing_run_id,
            grounding_ok,
        )
    )


def current_eligible_runs(
    session: Session, source_ids: Sequence[str] | None = None
) -> dict[str, str]:
    """Map source id -> current run id for sources that may be presented right now.

    Applies currency (rules 1-2) and policy (rule 3) but not grounding, which is a
    per-claim property. Deleted sources are filtered by joining `Source` rather than by a
    second query, so a source deleted between the two would not slip through.
    """
    stmt = (
        select(
            SourceProcessingState.source_id,
            SourceProcessingState.current_processing_run_id,
        )
        .join(Source, Source.id == SourceProcessingState.source_id)
        .where(
            SourceProcessingState.current_processing_run_id.is_not(None),
            SourceProcessingState.current_policy_action.not_in(sorted(HIDDEN_ACTIONS)),
            Source.locally_deleted_at_ms.is_(None),
        )
    )
    if source_ids:
        stmt = stmt.where(SourceProcessingState.source_id.in_(list(source_ids)))
    return {row[0]: row[1] for row in session.execute(stmt) if row[1]}


def is_source_eligible(session: Session, source_id: str) -> bool:
    """Whether this single source may currently contribute knowledge."""
    return source_id in current_eligible_runs(session, [source_id])


def eligible_claims(session: Session, stmt: Select[tuple[Claim]]) -> list[Claim]:
    """Narrow a `Claim` select down to claims presentable as knowledge.

    Filtering in Python rather than as a correlated join is deliberate: the currency check
    compares two columns from different tables for equality per row, and the readable SQL
    for that is a join the ORM would then have to de-duplicate. Claim volume here is bounded
    by one entity's claims, not by the corpus.
    """
    current = current_eligible_runs(session)
    if not current:
        return []
    return [
        claim
        for claim in session.scalars(stmt)
        if current.get(claim.source_id) == claim.processing_run_id
        and is_assertable(claim.grounding_status)
    ]


def eligible_claim_ids(session: Session, claim_ids: Sequence[str]) -> set[str]:
    """Subset of `claim_ids` that may currently be presented as knowledge.

    Used by surfaces that already hold claim ids -- wiki supports, Resurface cards -- and
    need to know which of them still count, without re-deriving the claims themselves.
    """
    if not claim_ids:
        return set()
    claims = eligible_claims(
        session, select(Claim).where(Claim.id.in_(list(claim_ids)))
    )
    return {claim.id for claim in claims}
