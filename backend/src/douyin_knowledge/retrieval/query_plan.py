"""The validated QueryPlan: the only thing the structured executor will run.

A plan is data, never SQL. A model may *propose* one (see ``query_parser``), but what
reaches the database is a set of typed constraints whose field names, operators and
value types were checked against closed whitelists first (ARCHITECTURE 19: "The model
should not receive raw database credentials and should not generate arbitrary SQL for
execution").

The validation boundary is deliberately unforgiving. :func:`validate_plan` raises
:class:`QueryPlanError` rather than dropping the offending constraint, because a plan
that silently loses ``price < 100`` answers a different question than the user asked
and looks like a correct answer while doing it. Callers that want degradation instead
of failure can catch the error and fall back to unstructured retrieval -- that choice
belongs to the caller, not to the validator.

Field vocabulary is intentionally small and matches *the implemented schema*, not the
prose in RETRIEVAL.md 5. The docs write ``entity_types: [restaurant]``, ``price_max``
and ``district: Mong Kok``; the schema has ``entity_type='place'`` with
``subtype='restaurant'``, a ``price_per_person`` claim predicate, and Chinese surface
forms in the corpus. Adapting to the schema is the instruction; the conflict is
recorded in DEC-020.

Every field on :class:`QueryPlan` is executed. That is a stronger promise than it
sounds, and it is why ``visited`` is absent from :class:`UserStateConstraint` and why
the parser no longer proposes ``entity_subtypes``: a plan field the executor ignores
tells the user their filter was understood while returning results that disregard it,
which is worse than not modelling the filter at all. The remaining fields that do not
narrow the result set -- ``semantic_requirements``, ``text_requirements`` -- are
ranking signals by contract and are documented as such, not hard constraints wearing
a constraint's clothes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from douyin_knowledge.knowledge.resurface import INTENT_STATES
from douyin_knowledge.retrieval.vocabulary import canonical_cuisine, canonical_district

__all__ = [
    "CLAIM_FIELDS",
    "ENTITY_TYPES",
    "INTENTS",
    "NUMERIC_OPERATORS",
    "USER_STATES",
    "ClaimConstraint",
    "LocationConstraint",
    "QueryPlan",
    "QueryPlanError",
    "TEXT_OPERATORS",
    "UserStateConstraint",
    "validate_plan",
]


class QueryPlanError(ValueError):
    """A proposed plan is not executable. Carries a machine-readable `code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


#: Exactly the operators the executor implements for numeric claim constraints.
#: Anything else -- ``!=``, ``between``, ``like``, ``in``, a SQL fragment -- is a
#: validation failure, not a best-effort translation.
NUMERIC_OPERATORS = frozenset({"<", "<=", ">", ">=", "="})

#: Text constraints support equality only in V1. No ``contains``, because a substring
#: match over claim values re-introduces the fuzzy behaviour this slice exists to avoid.
TEXT_OPERATORS = frozenset({"="})

#: Recognized intents. ``find`` and ``recommend`` differ in presentation only for now;
#: keeping them distinct means the answer layer can diverge later without a plan change.
INTENTS = frozenset({"find", "recommend", "explain"})

#: Claim fields the executor knows how to filter on, and the claim predicate plus
#: column each maps to. ``column`` is the *typed* column, never JSON (AGENTS 12).
CLAIM_FIELDS: dict[str, dict[str, str]] = {
    "price_per_person": {"predicate": "price_per_person", "column": "value_number", "kind": "number"},
    "cuisine": {"predicate": "cuisine", "column": "value_text", "kind": "text"},
    "district": {"predicate": "located_in", "column": "value_text", "kind": "text"},
}

#: Entity types the plan may ask for, mirroring the extractor's allowed set.
ENTITY_TYPES = frozenset({"place", "dish", "person", "brand", "tool", "topic", "unknown"})

MAX_LIMIT = 50
"""Hard ceiling on result count. The executor is bounded by construction."""


@dataclass(frozen=True)
class ClaimConstraint:
    """One typed, source-attributed constraint over a claim predicate.

    `field` is a key of :data:`CLAIM_FIELDS`, not a column name, so a plan can never
    name an arbitrary column. `required` distinguishes "must have a qualifying claim"
    from "prefer one", which is what lets the no-result path say *why* a candidate
    failed instead of reporting a bare miss.
    """

    field: str
    operator: str
    value_number: float | None = None
    value_text: str | None = None
    required: bool = True

    @property
    def kind(self) -> str:
        return CLAIM_FIELDS[self.field]["kind"]

    @property
    def predicate(self) -> str:
        return CLAIM_FIELDS[self.field]["predicate"]

    def describe(self) -> str:
        value = self.value_number if self.kind == "number" else self.value_text
        return f"{self.field} {self.operator} {value}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "operator": self.operator,
            "value_number": self.value_number,
            "value_text": self.value_text,
            "required": self.required,
        }


@dataclass(frozen=True)
class LocationConstraint:
    """A district (and optionally city) constraint, already canonicalized.

    Kept separate from :class:`ClaimConstraint` because location is how the user thinks
    about the query even though it executes as a ``located_in`` claim. Collapsing it
    into a generic claim constraint would make the diagnostics unreadable.
    """

    district: str
    city: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"district": self.district, "city": self.city}


#: User states a plan may filter on. This is exactly `resurface.INTENT_STATES`, imported
#: rather than restated so the two cannot drift: those are the states the product can
#: actually *write*, through `POST /api/knowledge/entities/{id}/user-state`.
#:
#: `visited` is deliberately absent, and its absence is the point. There is no write path
#: for it and `resurface.INTENT_STATES` excludes it on purpose ("finishing something is a
#: different feature"). Parsing 去过 into a constraint the executor cannot satisfy would
#: produce a plan that claims to filter on history and silently returns everything --
#: worse than not understanding the question, because the user cannot tell.
USER_STATES = INTENT_STATES


@dataclass(frozen=True)
class UserStateConstraint:
    """Filter on the user's own declared relationship to the entity.

    Separate from claims by design (AGENTS 4.4): "想去" is a user fact, "人均 80" is a
    creator assertion, and mixing them would let one overwrite the other.

    `present` distinguishes 想去的店 from 还没标想去的店 without needing a second field.
    """

    state: str
    present: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "present": self.present}

    def describe(self) -> str:
        return f"user_state {'=' if self.present else '!='} {self.state}"


@dataclass(frozen=True)
class QueryPlan:
    """A validated, executable query plan.

    Construct through :func:`validate_plan`. Building one directly bypasses the
    whitelist checks, which is acceptable inside tests that are asserting executor
    behaviour but never on the request path.
    """

    raw_query: str
    intent: str = "find"
    scope: str = "personal_first"
    entity_types: tuple[str, ...] = ()
    entity_subtypes: tuple[str, ...] = ()
    location: LocationConstraint | None = None
    claim_constraints: tuple[ClaimConstraint, ...] = ()
    text_requirements: tuple[str, ...] = ()
    semantic_requirements: tuple[str, ...] = ()
    user_state: UserStateConstraint | None = None
    conversation_entity_refs: tuple[str, ...] = ()
    limit: int = 10
    parser: str = "deterministic"
    notes: tuple[str, ...] = field(default=())

    @property
    def is_structured(self) -> bool:
        """Whether this plan has any hard constraint worth running the executor for.

        A plan with no location, no claim constraint and no user state is just a text
        query wearing a plan's clothes; routing it through the structured path would
        return every entity in the collection.

        `entity_types` alone does not qualify, and `entity_subtypes` no longer does
        either. Both are *narrowing* constraints -- they say which of the candidates
        found some other way may survive -- and neither is selective enough to generate
        candidates from on its own: "every place in the collection" is not a query
        result, it is a table scan with an answer attached.
        """
        return bool(self.location or self.required_constraints or self.user_state)

    @property
    def required_constraints(self) -> tuple[ClaimConstraint, ...]:
        return tuple(c for c in self.claim_constraints if c.required)

    def constraint_for(self, field_name: str) -> ClaimConstraint | None:
        for constraint in self.claim_constraints:
            if constraint.field == field_name:
                return constraint
        return None

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics/persistence shape. This is what lands in `messages.query_plan_json`."""
        return {
            "raw_query": self.raw_query,
            "intent": self.intent,
            "scope": self.scope,
            "entity_types": list(self.entity_types),
            "entity_subtypes": list(self.entity_subtypes),
            "location": self.location.as_dict() if self.location else None,
            "claim_constraints": [c.as_dict() for c in self.claim_constraints],
            "text_requirements": list(self.text_requirements),
            "semantic_requirements": list(self.semantic_requirements),
            "user_state": self.user_state.as_dict() if self.user_state else None,
            "conversation_entity_refs": list(self.conversation_entity_refs),
            "limit": self.limit,
            "parser": self.parser,
            "is_structured": self.is_structured,
            "notes": list(self.notes),
        }


def _as_str_tuple(value: Any, code: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise QueryPlanError(code, f"expected a list of strings, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise QueryPlanError(code, f"expected string items, got {type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    return tuple(out)


def _validate_claim_constraint(raw: Any) -> ClaimConstraint:
    if not isinstance(raw, dict):
        raise QueryPlanError(
            "constraint_not_object", f"constraint must be an object, got {type(raw).__name__}"
        )

    field_name = raw.get("field")
    if not isinstance(field_name, str) or field_name not in CLAIM_FIELDS:
        raise QueryPlanError(
            "unsupported_field",
            f"{field_name!r} is not a supported constraint field; "
            f"supported: {sorted(CLAIM_FIELDS)}",
        )

    spec = CLAIM_FIELDS[field_name]
    operator = raw.get("operator", "=")
    allowed = NUMERIC_OPERATORS if spec["kind"] == "number" else TEXT_OPERATORS
    if not isinstance(operator, str) or operator not in allowed:
        raise QueryPlanError(
            "unsupported_operator",
            f"{operator!r} is not supported for {field_name}; allowed: {sorted(allowed)}",
        )

    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise QueryPlanError("bad_required_flag", "`required` must be a boolean")

    if spec["kind"] == "number":
        value = raw.get("value", raw.get("value_number"))
        # bool is a subclass of int; `price < True` is not a query anyone meant to write.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryPlanError(
                "bad_value_type",
                f"{field_name} needs a numeric value, got {type(value).__name__}",
            )
        if value != value or value in (float("inf"), float("-inf")):  # NaN or infinity
            raise QueryPlanError("bad_value_type", f"{field_name} needs a finite number")
        return ClaimConstraint(
            field=field_name, operator=operator, value_number=float(value), required=required
        )

    value = raw.get("value", raw.get("value_text"))
    if not isinstance(value, str) or not value.strip():
        raise QueryPlanError(
            "bad_value_type", f"{field_name} needs a non-empty string value"
        )

    if field_name == "cuisine":
        canonical = canonical_cuisine(value)
        if canonical is None:
            raise QueryPlanError(
                "unknown_cuisine",
                f"{value!r} is outside the V1 cuisine vocabulary",
            )
        value = canonical
    elif field_name == "district":
        canonical = canonical_district(value)
        if canonical is None:
            raise QueryPlanError(
                "unknown_district",
                f"{value!r} is outside the V1 district vocabulary",
            )
        value = canonical

    return ClaimConstraint(
        field=field_name, operator=operator, value_text=value, required=required
    )


def validate_plan(raw: dict[str, Any], *, raw_query: str, scope: str) -> QueryPlan:
    """Validate a proposed plan into an executable one, or raise :class:`QueryPlanError`.

    `scope` is passed in rather than read from `raw` because scope is decided by
    :mod:`douyin_knowledge.conversation.scope` from the user's own words and is a trust
    boundary (RET-002). A model proposing its own scope could downgrade
    ``personal_required`` to ``general`` and answer from general knowledge -- exactly
    the failure the scope classifier exists to prevent.
    """
    if not isinstance(raw, dict):
        raise QueryPlanError("plan_not_object", "plan must be an object")

    intent = raw.get("intent", "find")
    if not isinstance(intent, str) or intent not in INTENTS:
        raise QueryPlanError(
            "unsupported_intent", f"{intent!r} is not a supported intent; allowed: {sorted(INTENTS)}"
        )

    entity_types = _as_str_tuple(raw.get("entity_types"), "bad_entity_types")
    for entity_type in entity_types:
        if entity_type not in ENTITY_TYPES:
            raise QueryPlanError(
                "unsupported_entity_type",
                f"{entity_type!r} is not a known entity_type; allowed: {sorted(ENTITY_TYPES)}",
            )
    entity_subtypes = _as_str_tuple(raw.get("entity_subtypes"), "bad_entity_subtypes")

    location: LocationConstraint | None = None
    raw_location = raw.get("location")
    if raw_location is not None:
        if not isinstance(raw_location, dict):
            raise QueryPlanError("bad_location", "location must be an object")
        district_raw = raw_location.get("district")
        if district_raw is not None:
            if not isinstance(district_raw, str):
                raise QueryPlanError("bad_location", "location.district must be a string")
            district = canonical_district(district_raw)
            if district is None:
                raise QueryPlanError(
                    "unknown_district", f"{district_raw!r} is outside the V1 district vocabulary"
                )
            city_raw = raw_location.get("city")
            if city_raw is not None and not isinstance(city_raw, str):
                raise QueryPlanError("bad_location", "location.city must be a string")
            location = LocationConstraint(district=district, city=city_raw)

    constraints = raw.get("claim_constraints") or []
    if not isinstance(constraints, (list, tuple)):
        raise QueryPlanError("bad_constraints", "claim_constraints must be a list")
    validated = tuple(_validate_claim_constraint(item) for item in constraints)

    seen_fields: set[str] = set()
    for constraint in validated:
        if constraint.field in seen_fields:
            # Two constraints on one field would need conjunction semantics the
            # executor does not implement; failing is honest, silently keeping the
            # last one is not.
            raise QueryPlanError(
                "duplicate_constraint", f"more than one constraint on {constraint.field}"
            )
        seen_fields.add(constraint.field)

    raw_state = raw.get("user_state")
    user_state: UserStateConstraint | None = None
    if raw_state is not None:
        if not isinstance(raw_state, dict):
            raise QueryPlanError("bad_user_state", "user_state must be an object")
        state = raw_state.get("state")
        if not isinstance(state, str) or state not in USER_STATES:
            # `visited` lands here, by design. It is not a typo to fix later: there is no
            # write path for it, so accepting it would mean accepting a filter that can
            # never be true and answering 去过的店 with an empty list that looks like a
            # real result.
            raise QueryPlanError(
                "unsupported_user_state",
                f"{state!r} is not a supported user state; supported: {sorted(USER_STATES)}",
            )
        present = raw_state.get("present", True)
        if not isinstance(present, bool):
            raise QueryPlanError("bad_user_state", "user_state.present must be a boolean")
        user_state = UserStateConstraint(state=state, present=present)

    limit = raw.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise QueryPlanError("bad_limit", "limit must be a positive integer")
    limit = min(limit, MAX_LIMIT)

    parser = raw.get("parser", "deterministic")
    if not isinstance(parser, str) or not parser:
        raise QueryPlanError("bad_parser", "parser must be a non-empty string")

    return QueryPlan(
        raw_query=raw_query,
        intent=intent,
        scope=scope,
        entity_types=entity_types,
        entity_subtypes=entity_subtypes,
        location=location,
        claim_constraints=validated,
        text_requirements=_as_str_tuple(raw.get("text_requirements"), "bad_text_requirements"),
        semantic_requirements=_as_str_tuple(
            raw.get("semantic_requirements"), "bad_semantic_requirements"
        ),
        user_state=user_state,
        conversation_entity_refs=_as_str_tuple(
            raw.get("conversation_entity_refs"), "bad_conversation_refs"
        ),
        limit=limit,
        parser=parser,
        notes=_as_str_tuple(raw.get("notes"), "bad_notes"),
    )
