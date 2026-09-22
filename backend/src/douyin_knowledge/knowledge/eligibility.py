"""The one definition of "current normal knowledge".

A claim may be presented as knowledge only when *all* of the following hold:

1. its source is not locally deleted;
2. its source has a current processing-run pointer (DB-004);
3. the source's current policy action is not in ``HIDDEN_ACTIONS``;
4. the claim belongs to that current run;
5. the claim is assertable, i.e. grounding validation did not downgrade it (DEC-017);
6. the claim has at least one evidence unit drawn from its *own* source.

Rules 1-2 are currency, rule 3 is policy, rules 5-6 are grounding. Three different concerns,
one predicate, because a surface that applies two of the three is a leak. That is not
hypothetical: the Knowledge API applied only 1, 2 and 4, so making a source
``metadata_only`` removed it from Ask and search while its claims stayed on entity pages.

Rule 6 fails closed on inconsistent persisted data. ``GroundingValidator.validate``
already refuses to persist a claim whose evidence comes from another source -- it returns
``rejected_context`` -- so a claim with no same-source evidence should not exist. When one
does exist anyway (a deleted link, a repointed row, a partial write), the grounding
contract behind rules 5 and 6 has been violated, and the only safe reading of an
unverifiable claim is that it is not knowledge. Disqualifying, not downgrading, so that
retrieval, Wiki and Resurface all reach the same verdict the validator would have.

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
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit
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
    """Add the six eligibility rules to a ``Claim`` select as SQL.

    The same predicate as :func:`eligible_claims`, expressed as joins instead of a
    Python comprehension. Two implementations of one rule is a drift risk, and it is
    accepted here for a specific reason: :func:`eligible_claims` loads every row its
    statement matches, which its docstring bounds at "one entity's claims, not the
    corpus". Structured retrieval starts from a *predicate* (`price_per_person` over
    every entity) and that bound does not hold, so the filter has to happen in SQLite
    where ``LIMIT`` can also apply.

    The drift risk is contained by ``test_structured_retrieval.py``, which asserts both
    functions return the same claim ids over a fixture built to exercise every rule.
    Grounding statuses come from :data:`ASSERTABLE_STATUSES` rather than being re-listed,
    and the ``NULL`` member needs its own ``IS NULL`` term because SQL ``IN`` never
    matches ``NULL``.

    Rule 6 is an ``EXISTS`` rather than a join to ``claim_evidence`` on purpose. A claim
    normally has several evidence rows, and a join would emit one output row per row --
    callers here apply ``LIMIT MAX_CANDIDATES * 8``, so the duplicates would silently eat
    the candidate budget and drop real entities off the end. ``EXISTS`` answers the only
    question the rule asks (is there *any* same-source evidence) without changing
    cardinality.
    """
    statuses = [s for s in ASSERTABLE_STATUSES if s is not None]
    grounding_ok = Claim.grounding_status.in_(sorted(statuses))
    if None in ASSERTABLE_STATUSES:
        grounding_ok = or_(grounding_ok, Claim.grounding_status.is_(None))

    same_source_evidence = (
        select(1)
        .select_from(ClaimEvidence)
        .join(EvidenceUnit, EvidenceUnit.id == ClaimEvidence.evidence_id)
        .where(
            ClaimEvidence.claim_id == Claim.id,
            EvidenceUnit.source_id == Claim.source_id,
        )
        .exists()
    )

    return (
        stmt.join(SourceProcessingState, SourceProcessingState.source_id == Claim.source_id)
        .join(Source, Source.id == Claim.source_id)
        .where(
            Source.locally_deleted_at_ms.is_(None),
            SourceProcessingState.current_processing_run_id.is_not(None),
            SourceProcessingState.current_policy_action.not_in(sorted(HIDDEN_ACTIONS)),
            SourceProcessingState.current_processing_run_id == Claim.processing_run_id,
            grounding_ok,
            same_source_evidence,
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

    Rule 6 is resolved with one prefetch query returning the claim ids that *do* have
    same-source evidence, mirroring the shape :func:`current_eligible_runs` already uses
    for rules 1-3. Asking per claim would be an N+1 over a list this function is otherwise
    careful to load in a single round trip.
    """
    current = current_eligible_runs(session)
    if not current:
        return []
    claims = list(session.scalars(stmt))
    if not claims:
        return []
    grounded_claim_ids = set(
        session.scalars(
            select(Claim.id)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(EvidenceUnit, EvidenceUnit.id == ClaimEvidence.evidence_id)
            .where(
                Claim.id.in_(sorted(claim.id for claim in claims)),
                EvidenceUnit.source_id == Claim.source_id,
            )
            .distinct()
        )
    )
    return [
        claim
        for claim in claims
        if current.get(claim.source_id) == claim.processing_run_id
        and is_assertable(claim.grounding_status)
        and claim.id in grounded_claim_ids
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
