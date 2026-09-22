"""Deterministic structured execution of a validated QueryPlan.

This is the half of retrieval that similarity cannot override. A plan carrying
``price_per_person < 100`` means an entity qualifies only if an *eligible* claim says so;
a candidate whose text is a perfect embedding match for the question but whose current
price claim says 150 is rejected here and cannot be reinstated downstream
(INTEGRATION_PLAN_V1 §11: "Hard structured constraints must not be overridden by
FTS/vector similarity"). Structurally, that guarantee comes from ordering: the qualifying
entity set is computed first, and FTS/vector may only rank and illustrate what survives.

Three properties are worth reading the code for.

*Eligibility is inherited, not re-implemented.* Every claim this module considers comes
from :func:`apply_claim_eligibility`, so locally deleted sources, ``exclude``,
``metadata_only``, superseded runs and downgraded grounding are all filtered by the one
shared rule. The superseded-run case is the sharpest: a source reprocessed from 80 to 130
has both claims on disk, and the old one is invisible here because it belongs to a run
that is no longer current (DB-004).

*Conflicts are preserved, never resolved.* Two sources asserting 80 and 120 for one
restaurant produce two claims, both kept, both surfaced. The entity qualifies for
``< 100`` because eligible evidence satisfies the condition, and the answer must still
show the 120 (AGENTS 4.3, DB-005: structured filtering "does not convert them into
unquestioned global facts").

*Rejections are typed.* "Nothing found" is four or five different situations needing four
or five different user actions, so every candidate that fails carries the reason it failed
(RETRIEVAL.md 15).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, Entity
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.knowledge.eligibility import apply_claim_eligibility
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.retrieval.query_plan import CLAIM_FIELDS, ClaimConstraint, QueryPlan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = get_logger(__name__)

__all__ = [
    "MAX_CANDIDATES",
    "REJECTION_REASONS",
    "StructuredExecutor",
    "StructuredMatch",
    "StructuredResult",
    "StructuredRejection",
]

MAX_CANDIDATES = 500
"""Ceiling on candidate entities considered per query.

The executor is bounded by construction, not by hoping the corpus stays small. A
personal collection that hits this ceiling has other problems, but an unbounded scan
inside a request is a defect regardless of whether it currently triggers.
"""

#: Reverse of `CLAIM_FIELDS`: claim predicate -> plan field name. Rejections report the
#: *plan's* vocabulary ("district"), not the storage predicate ("located_in"), because a
#: diagnostic the user's question cannot be mapped back onto is not a diagnostic.
_FIELD_BY_PREDICATE = {spec["predicate"]: name for name, spec in CLAIM_FIELDS.items()}

#: Every reason a candidate can fail, as stable machine-readable strings. Tests assert
#: on these, so they are API: renaming one is a breaking change.
REJECTION_REASONS = frozenset(
    {
        "entity_type_mismatch",
        "entity_subtype_mismatch",
        "missing_required_claim",
        "numeric_constraint_failed",
        "text_constraint_failed",
        "user_state_mismatch",
        "no_eligible_claims_metadata_only",
        "no_eligible_claims_excluded",
        "no_eligible_claims_superseded",
        "no_eligible_claims_downgraded",
    }
)


@dataclass
class ConstraintSupport:
    """Which claims satisfied one constraint, and every eligible value seen for it.

    `conflicting_values` is the whole point of this dataclass existing rather than a bare
    claim list. The satisfying claim answers "does it qualify"; the full value set answers
    "is that the settled truth", and the second question is the one the user actually
    needs answered when two videos disagree.
    """

    field: str
    satisfied_by: list[Claim] = dataclasses.field(default_factory=list)
    all_eligible: list[Claim] = dataclasses.field(default_factory=list)

    @property
    def conflicting_values(self) -> list[Any]:
        """Distinct eligible values for this field, ordered, when there is more than one."""
        values: list[Any] = []
        for claim in self.all_eligible:
            value = claim.value_number if claim.value_number is not None else claim.value_text
            if value is not None and value not in values:
                values.append(value)
        return values if len(values) > 1 else []

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting_values)


@dataclass
class StructuredMatch:
    """One entity that satisfied every hard constraint, with its supporting claims."""

    entity: Entity
    supports: dict[str, ConstraintSupport] = field(default_factory=dict)

    @property
    def claims(self) -> list[Claim]:
        """Every eligible claim behind this match, satisfying or conflicting.

        Conflicting claims are included deliberately: they have to reach the citation
        builder or the answer cannot show the disagreement it is required to show.
        """
        seen: dict[str, Claim] = {}
        for support in self.supports.values():
            for claim in [*support.satisfied_by, *support.all_eligible]:
                seen.setdefault(claim.id, claim)
        return list(seen.values())

    @property
    def source_ids(self) -> list[str]:
        out: list[str] = []
        for claim in self.claims:
            if claim.source_id not in out:
                out.append(claim.source_id)
        return out

    @property
    def conflicts(self) -> dict[str, list[Any]]:
        return {
            field_name: support.conflicting_values
            for field_name, support in self.supports.items()
            if support.has_conflict
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity.id,
            "entity_name": self.entity.canonical_name,
            "entity_type": self.entity.entity_type,
            "subtype": self.entity.subtype,
            "supports": {
                name: {
                    "satisfied_by": [c.id for c in support.satisfied_by],
                    "eligible_claims": [c.id for c in support.all_eligible],
                    "conflicting_values": support.conflicting_values,
                }
                for name, support in self.supports.items()
            },
            "source_ids": self.source_ids,
        }


@dataclass
class StructuredRejection:
    """A candidate that did not qualify, and why.

    Kept for every rejected candidate rather than only counted, because
    "why was this excluded?" is a question the tests and the debug surface both have to
    answer, and a count cannot answer it.
    """

    entity_id: str
    entity_name: str
    reason: str
    detail: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "reason": self.reason,
            "field": self.field,
            "detail": self.detail,
        }


@dataclass
class StructuredResult:
    """Qualifying entities, rejected candidates, and how the executor got there."""

    plan: QueryPlan
    matches: list[StructuredMatch] = field(default_factory=list)
    rejections: list[StructuredRejection] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def qualifying_source_ids(self) -> list[str]:
        """Sources behind qualifying entities. The allow-list for FTS/vector support."""
        out: list[str] = []
        for match in self.matches:
            for source_id in match.source_ids:
                if source_id not in out:
                    out.append(source_id)
        return out

    @property
    def claims(self) -> list[Claim]:
        seen: dict[str, Claim] = {}
        for match in self.matches:
            for claim in match.claims:
                seen.setdefault(claim.id, claim)
        return list(seen.values())

    def is_empty(self) -> bool:
        return not self.matches

    def as_dict(self) -> dict[str, Any]:
        return {
            "matches": [m.as_dict() for m in self.matches],
            "rejections": [r.as_dict() for r in self.rejections],
            **self.diagnostics,
        }


def _numeric_satisfied(value: float | None, operator: str, target: float) -> bool:
    """Apply one whitelisted numeric operator.

    The operator reached here through :func:`validate_plan`'s whitelist, so an unknown
    one is an internal inconsistency rather than bad input -- and returning ``False`` for
    it is the safe direction: an unimplemented operator must exclude, never include.
    """
    if value is None:
        return False
    if operator == "<":
        return value < target
    if operator == "<=":
        return value <= target
    if operator == ">":
        return value > target
    if operator == ">=":
        return value >= target
    if operator == "=":
        return value == target
    logger.error("unknown_numeric_operator", extra={"operator": operator})
    return False


class StructuredExecutor:
    """Run a validated plan against the Entity/Claim/Evidence/Source spine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------- candidates

    def _eligible_claims_for(
        self, predicates: Sequence[str], *, entity_ids: Sequence[str] | None = None
    ) -> list[Claim]:
        """Every eligible claim on `predicates`, optionally narrowed to some entities.

        Eligibility is applied in SQL (see :func:`apply_claim_eligibility`) so ``LIMIT``
        is meaningful. Doing it in Python here would mean loading every price claim in
        the corpus before discarding the excluded ones.
        """
        if not predicates:
            return []
        stmt = select(Claim).where(
            Claim.predicate.in_(list(predicates)),
            Claim.subject_entity_id.is_not(None),
        )
        if entity_ids is not None:
            if not entity_ids:
                return []
            stmt = stmt.where(Claim.subject_entity_id.in_(list(entity_ids)))
        stmt = apply_claim_eligibility(stmt).limit(MAX_CANDIDATES * 8)
        return list(self.session.scalars(stmt))

    def _candidate_entity_ids(self, plan: QueryPlan) -> tuple[list[str], dict[str, Any]]:
        """Entity ids worth evaluating, derived from the plan's own hard constraints.

        Candidate generation runs off the *most selective* constraint available rather
        than scanning the entity table: a district or cuisine constraint names a claim
        predicate, and the entities carrying it are the only ones that can possibly
        qualify. Starting from entities and filtering afterwards would read the whole
        corpus to answer a two-restaurant question.
        """
        diagnostics: dict[str, Any] = {}
        selective_predicates: list[str] = []
        if plan.location is not None:
            selective_predicates.append("located_in")
        for constraint in plan.required_constraints:
            if constraint.kind == "text":
                selective_predicates.append(constraint.predicate)

        if selective_predicates:
            claims = self._eligible_claims_for(selective_predicates)
            ids: list[str] = []
            for claim in claims:
                if claim.subject_entity_id and claim.subject_entity_id not in ids:
                    ids.append(claim.subject_entity_id)
            diagnostics["candidate_source"] = "text_constraints"
            diagnostics["candidate_predicates"] = sorted(set(selective_predicates))
            return ids[:MAX_CANDIDATES], diagnostics

        numeric_predicates = [
            c.predicate for c in plan.required_constraints if c.kind == "number"
        ]
        if numeric_predicates:
            claims = self._eligible_claims_for(numeric_predicates)
            ids = []
            for claim in claims:
                if claim.subject_entity_id and claim.subject_entity_id not in ids:
                    ids.append(claim.subject_entity_id)
            diagnostics["candidate_source"] = "numeric_constraints"
            diagnostics["candidate_predicates"] = sorted(set(numeric_predicates))
            return ids[:MAX_CANDIDATES], diagnostics

        # Subtype-only plan: no claim predicate to start from, so the entity table is
        # the candidate set. Bounded by MAX_CANDIDATES like every other path.
        stmt = select(Entity.id).where(Entity.status == "active")
        if plan.entity_types:
            stmt = stmt.where(Entity.entity_type.in_(list(plan.entity_types)))
        if plan.entity_subtypes:
            stmt = stmt.where(Entity.subtype.in_(list(plan.entity_subtypes)))
        diagnostics["candidate_source"] = "entity_table"
        return list(self.session.scalars(stmt.limit(MAX_CANDIDATES))), diagnostics

    # ------------------------------------------------------------- diagnosis

    def _diagnose_ineligibility(self, entity_id: str, predicate: str) -> tuple[str, str] | None:
        """Explain why an entity has no *eligible* claim on `predicate`, if it has any at all.

        This is the difference between "you never saved anything about this" and "you
        saved it but told the system to skip it", and the two need opposite user actions.
        The query deliberately bypasses :func:`apply_claim_eligibility` -- it is asking
        about claims that failed eligibility, so filtering them out first would make the
        question unanswerable.
        """
        rows = self.session.execute(
            select(
                Claim.grounding_status,
                Claim.processing_run_id,
                SourceProcessingState.current_policy_action,
                SourceProcessingState.current_processing_run_id,
                Source.locally_deleted_at_ms,
            )
            .join(Source, Source.id == Claim.source_id)
            .outerjoin(SourceProcessingState, SourceProcessingState.source_id == Claim.source_id)
            .where(Claim.subject_entity_id == entity_id, Claim.predicate == predicate)
        ).all()
        if not rows:
            return None

        for grounding, run_id, action, current_run, deleted in rows:
            if action == "metadata_only":
                return (
                    "no_eligible_claims_metadata_only",
                    "来源只保留了元数据，内容从未被真正理解",
                )
            if action == "exclude":
                return ("no_eligible_claims_excluded", "来源已被排除在知识检索之外")
            if deleted is not None:
                return ("no_eligible_claims_excluded", "来源已在本地删除")
            if current_run is not None and run_id != current_run:
                return (
                    "no_eligible_claims_superseded",
                    "该数值来自已被取代的旧处理结果",
                )
            if grounding is not None and grounding != "valid":
                return ("no_eligible_claims_downgraded", f"断言未通过溯源校验（{grounding}）")
        return ("no_eligible_claims_superseded", "没有属于当前处理结果的断言")

    def _diagnose_empty_candidates(self, plan: QueryPlan) -> list[StructuredRejection]:
        """Rejections for entities that would have matched but for eligibility.

        Only entities whose *ineligible* claims match the plan's text constraints by
        value are reported. Reporting every entity with any ``located_in`` claim would
        answer a question about 旺角 by naming an excluded restaurant in 中环, which is
        a worse failure than saying nothing: it sends the user to the wrong video.
        """
        wanted: dict[str, str] = {}
        if plan.location is not None:
            wanted["located_in"] = plan.location.district
        for constraint in plan.required_constraints:
            if constraint.kind == "text" and constraint.value_text is not None:
                wanted[constraint.predicate] = constraint.value_text
        if not wanted:
            return []

        rows = self.session.execute(
            select(Claim.subject_entity_id, Claim.predicate, Claim.value_text)
            .where(
                Claim.predicate.in_(sorted(wanted)),
                Claim.subject_entity_id.is_not(None),
            )
            .limit(MAX_CANDIDATES * 8)
        ).all()

        entity_ids: list[str] = []
        for entity_id, predicate, value_text in rows:
            if value_text is None or entity_id is None:
                continue
            if value_text.strip().casefold() != wanted[predicate].casefold():
                continue
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
        if not entity_ids:
            return []

        names = {
            row.id: row.canonical_name
            for row in self.session.scalars(
                select(Entity).where(Entity.id.in_(entity_ids[:MAX_CANDIDATES]))
            )
        }
        rejections: list[StructuredRejection] = []
        for entity_id in entity_ids[:MAX_CANDIDATES]:
            for predicate in sorted(wanted):
                diagnosis = self._diagnose_ineligibility(entity_id, predicate)
                if diagnosis is None:
                    continue
                reason, detail = diagnosis
                rejections.append(
                    StructuredRejection(
                        entity_id=entity_id,
                        entity_name=names.get(entity_id, entity_id),
                        reason=reason,
                        detail=detail,
                        field=_FIELD_BY_PREDICATE.get(predicate, predicate),
                    )
                )
                break
        return rejections

    # ------------------------------------------------------------- evaluation

    def _evaluate_constraint(
        self, constraint: ClaimConstraint, claims: list[Claim]
    ) -> ConstraintSupport:
        """Which of `claims` satisfy `constraint`. All eligible ones are retained."""
        support = ConstraintSupport(field=constraint.field, all_eligible=list(claims))
        for claim in claims:
            if constraint.kind == "number":
                assert constraint.value_number is not None  # guaranteed by validation
                if _numeric_satisfied(
                    claim.value_number, constraint.operator, constraint.value_number
                ):
                    support.satisfied_by.append(claim)
            elif claim.value_text is not None and constraint.value_text is not None:
                # Text constraints compare canonical vocabulary values, which the writer
                # normalized on the way in and `validate_plan` normalized on the way out,
                # so this is an exact match by construction rather than by luck.
                if claim.value_text.strip().casefold() == constraint.value_text.casefold():
                    support.satisfied_by.append(claim)
        return support

    def execute(self, plan: QueryPlan) -> StructuredResult:
        """Run `plan`. Returns qualifying entities plus a typed reason for every rejection."""
        result = StructuredResult(plan=plan)
        if not plan.is_structured:
            result.diagnostics["reason"] = "plan_not_structured"
            return result

        candidate_ids, candidate_diagnostics = self._candidate_entity_ids(plan)
        result.diagnostics.update(candidate_diagnostics)
        result.diagnostics["candidates"] = len(candidate_ids)
        if not candidate_ids:
            # Zero *eligible* candidates is not the same statement as "you have nothing
            # about this". A metadata_only or excluded source produces claims that match
            # the plan perfectly and are filtered before candidate generation ever sees
            # them, so the honest answer is "you saved it but it was never understood"
            # -- which needs a different user action than "nothing found" (brief 7).
            result.rejections += self._diagnose_empty_candidates(plan)
            result.diagnostics["rejected"] = len(result.rejections)
            result.diagnostics["rejection_reasons"] = sorted(
                {r.reason for r in result.rejections}
            )
            result.diagnostics["reason"] = (
                "all_candidates_ineligible"
                if result.rejections
                else "no_structured_candidates"
            )
            return result

        entities = {
            entity.id: entity
            for entity in self.session.scalars(
                select(Entity).where(Entity.id.in_(candidate_ids))
            )
        }

        # Every predicate the plan needs, fetched once for all candidates rather than
        # per entity: N+1 queries inside a request is the same defect as an unbounded scan.
        predicates: list[str] = ["located_in"] if plan.location else []
        predicates += [c.predicate for c in plan.claim_constraints]
        claims_by_entity: dict[str, dict[str, list[Claim]]] = {}
        for claim in self._eligible_claims_for(predicates, entity_ids=candidate_ids):
            if claim.subject_entity_id:
                claims_by_entity.setdefault(claim.subject_entity_id, {}).setdefault(
                    claim.predicate, []
                ).append(claim)

        constraints = list(plan.claim_constraints)
        if plan.location is not None:
            constraints.append(
                ClaimConstraint(
                    field="district",
                    operator="=",
                    value_text=plan.location.district,
                    required=True,
                )
            )

        for entity_id in candidate_ids:
            entity = entities.get(entity_id)
            if entity is None:
                continue
            match = self._evaluate_entity(entity, constraints, claims_by_entity.get(entity_id, {}))
            if isinstance(match, StructuredRejection):
                result.rejections.append(match)
                continue
            result.matches.append(match)
            if len(result.matches) >= plan.limit:
                break

        result.diagnostics["matched"] = len(result.matches)
        result.diagnostics["rejected"] = len(result.rejections)
        result.diagnostics["rejection_reasons"] = sorted({r.reason for r in result.rejections})
        result.diagnostics["conflicts"] = {
            m.entity.canonical_name: m.conflicts for m in result.matches if m.conflicts
        }
        if not result.matches:
            result.diagnostics["reason"] = "all_candidates_rejected"
        return result

    def _evaluate_entity(
        self,
        entity: Entity,
        constraints: list[ClaimConstraint],
        claims: dict[str, list[Claim]],
    ) -> StructuredMatch | StructuredRejection:
        """One entity against every constraint. First failure wins and is reported.

        Entity ``subtype`` is deliberately *not* checked. A plan for the canonical query
        asks for ``subtype = restaurant``, and nothing in the pipeline currently writes
        ``subtype`` at all, so enforcing it would reject every restaurant in the corpus.
        Candidate generation already narrows by ``entity_type`` where it can. The gap is
        recorded in DEC-020 rather than papered over with a filter that silently matches
        nothing.
        """
        match = StructuredMatch(entity=entity)
        for constraint in constraints:
            available = claims.get(constraint.predicate, [])
            if not available:
                if not constraint.required:
                    continue
                diagnosis = self._diagnose_ineligibility(entity.id, constraint.predicate)
                if diagnosis is not None:
                    reason, detail = diagnosis
                    return StructuredRejection(
                        entity_id=entity.id,
                        entity_name=entity.canonical_name,
                        reason=reason,
                        detail=detail,
                        field=constraint.field,
                    )
                return StructuredRejection(
                    entity_id=entity.id,
                    entity_name=entity.canonical_name,
                    reason="missing_required_claim",
                    detail=f"没有关于 {constraint.field} 的当前有效断言",
                    field=constraint.field,
                )

            support = self._evaluate_constraint(constraint, available)
            if not support.satisfied_by and constraint.required:
                reason = (
                    "numeric_constraint_failed"
                    if constraint.kind == "number"
                    else "text_constraint_failed"
                )
                values = [
                    c.value_number if c.value_number is not None else c.value_text
                    for c in available
                ]
                return StructuredRejection(
                    entity_id=entity.id,
                    entity_name=entity.canonical_name,
                    reason=reason,
                    detail=f"{constraint.describe()} 不成立，现有有效值：{values}",
                    field=constraint.field,
                )
            match.supports[constraint.field] = support

        return match
