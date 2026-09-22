"""Natural language -> proposed QueryPlan.

Cues first, model second -- the pattern ``policy/triage.py`` already uses. The
deterministic parser is not a fallback for when the model is unavailable; it is the
primary path, because "人均100以下" has exactly one correct reading and asking a model
to re-derive it introduces variance into a filter the user can see. The model is only
consulted when the deterministic pass finds no structured constraint at all, and its
output goes through the same :func:`validate_plan` whitelist as everything else.

This ordering also happens to be the only one that works offline: ``ai_provider``
defaults to ``mock``, ``MockStructuredModel`` returns ``{}`` for any schema it does not
recognize, and the test suite strips every ``DK_*`` variable. A model-first parser would
be a no-op in exactly the configuration CI runs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from douyin_knowledge.extraction.heuristics import _NUM, parse_chinese_number
from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.retrieval.query_plan import (
    CLAIM_FIELDS,
    QueryPlan,
    QueryPlanError,
    validate_plan,
)
from douyin_knowledge.retrieval.vocabulary import CUISINE_ALIASES, DISTRICT_ALIASES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from douyin_knowledge.ai.providers import StructuredModel

logger = get_logger(__name__)

__all__ = ["parse_query", "propose_plan_deterministic", "PLAN_SCHEMA"]


# ----------------------------------------------------------------- price cues

#: Phrases that put an upper bound on price. Ordered longest-first within each
#: alternation so ``不超过`` wins over ``超过`` -- the latter is the opposite comparison
#: and a shorter-first scan would invert the user's filter.
_PRICE_UPPER = (
    "以下", "以内", "之内", "不超过", "不到", "低于", "少于", "便宜", "内",
)
_PRICE_UPPER_INCLUSIVE = ("以内", "之内", "不超过", "内")
_PRICE_LOWER = ("以上", "超过", "高于", "多于", "起")
_PRICE_LOWER_INCLUSIVE = ("以上", "起")
_PRICE_EXACT = ("正好", "刚好", "等于")

#: ``人均`` / ``均价`` / ``价格`` / ``预算`` all introduce a per-person budget in this corpus.
_PRICE_SUBJECT = r"(?:人均|均价|客单价|价格|预算|消费)"


def _price_constraint(query: str) -> dict[str, Any] | None:
    """Extract a single ``price_per_person`` constraint from Chinese price phrasing.

    Handles, deliberately and only: ``人均100以下`` / ``人均不超过100`` / ``人均100以内`` /
    ``100以内的人均`` / ``人均100以上`` / ``人均正好100``. Anything vaguer ("便宜的",
    "性价比高") is a *semantic* requirement, not a hard constraint, and is routed there
    instead -- inventing a threshold for "便宜" would fabricate the user's budget.
    """
    # Number adjacent to a price subject, in either order. The first pattern allows the
    # comparison marker to sit *between* subject and number, which is where Chinese puts
    # it in 人均不超过100 -- without this the phrase parses as an unconstrained price
    # mention and the user's ceiling silently disappears.
    # Markers are ordered longest-first inside the alternation. Regex alternation is
    # first-match, not longest-match, so listing "超过" before "不超过" would match the
    # former inside the latter and invert the comparison.
    markers = sorted(
        {*_PRICE_UPPER, *_PRICE_LOWER, *_PRICE_EXACT}, key=len, reverse=True
    )
    marker_group = "|".join(re.escape(m) for m in markers)
    patterns = (
        # 人均不超过100 / 人均大概80 -- marker (if any) between subject and number.
        rf"{_PRICE_SUBJECT}\s*(?:大概|大约|约|在)?\s*(?P<marker>{marker_group})?\s*(?P<num>{_NUM})",
        # 100以内的人均 -- number first, marker trailing.
        rf"(?P<num>{_NUM})\s*(?:元|块|块钱|港币|rmb)?\s*(?P<marker>{marker_group})?\s*的?\s*{_PRICE_SUBJECT}",
    )
    match: re.Match[str] | None = None
    for pattern in patterns:
        match = re.search(pattern, query)
        if match:
            break
    if match is None:
        return None

    amount = parse_chinese_number(match.group("num"))
    if amount is None:
        return None

    # A marker captured inside the match wins. Otherwise look just after the number, which
    # is where a trailing 以下/以内 lands: 人均100以下 puts the number inside the match and
    # the marker immediately behind it.
    inline = match.group("marker")
    after = query[match.end() : match.end() + 8]
    before = query[max(0, match.start() - 8) : match.start()]

    def _find(candidates: tuple[str, ...]) -> str | None:
        if inline is not None:
            return inline if inline in candidates else None
        for window in (after, before):
            for marker in sorted(candidates, key=len, reverse=True):
                if marker in window:
                    return marker
        return None

    exact = _find(_PRICE_EXACT)
    if exact is not None:
        return {"field": "price_per_person", "operator": "=", "value": amount}

    lower = _find(_PRICE_LOWER)
    upper = _find(_PRICE_UPPER)
    # When both windows contain markers, prefer the one that appears in `after`,
    # which is where Chinese normally puts the comparison for a trailing number.
    if lower is not None and (upper is None or lower in after):
        operator = ">=" if lower in _PRICE_LOWER_INCLUSIVE else ">"
        return {"field": "price_per_person", "operator": operator, "value": amount}
    if upper is not None:
        operator = "<=" if upper in _PRICE_UPPER_INCLUSIVE else "<"
        return {"field": "price_per_person", "operator": operator, "value": amount}
    return None


# --------------------------------------------------------- refused conditions

#: Words that negate the vocabulary term immediately following them.
_NEGATION_CUES = ("不是", "不要", "别", "非", "除了", "不想吃", "不吃")

#: How far after a negation cue a vocabulary hit still counts as negated. A short window,
#: because the cue has to be modifying *this* term: in 不是日料的旺角餐厅 the cue sits
#: directly against 日料, while 旺角 five characters later is not negated at all.
_NEGATION_WINDOW = 4


def _negated_vocabulary_term(query: str) -> str | None:
    """The first district or cuisine term the query negates, if any.

    Cuisine and district are matched by bare substring scan, which cannot tell
    ``不是日料`` from ``日料``. Left alone that inverts the user's meaning: asking for
    餐厅 that are *not* 日料 executed as ``cuisine = 日料`` and returned exactly the
    restaurants the user ruled out.
    """
    folded = query.casefold()
    for cue in _NEGATION_CUES:
        start = folded.find(cue)
        while start != -1:
            window = folded[start + len(cue) : start + len(cue) + _NEGATION_WINDOW]
            for aliases in (CUISINE_ALIASES, DISTRICT_ALIASES):
                for alias in sorted(aliases, key=len, reverse=True):
                    if alias in window:
                        return alias
            start = folded.find(cue, start + 1)
    return None


def _price_condition_count(query: str) -> int:
    """How many explicit price conditions the query states.

    A condition is a number carrying its own comparison marker. Counting them is not
    the same as parsing them: ``_price_constraint`` returns a single constraint, and its
    8-character lookahead window cannot tell 人均100以下和200以上 (two conditions) from
    人均100以下 (one). In that phrase the window held both 以下 and 以上, the
    lower-bound branch won, and the plan executed ``>= 100`` -- a predicate matching
    *neither* stated condition and admitting the whole 100-200 band the user excluded
    twice. Detecting the shape lets the caller refuse it.

    Deliberately blind to a number with no marker: 人均大概80 is vague by contract and
    must keep yielding no constraint and no error.
    """
    if not re.search(_PRICE_SUBJECT, query):
        return 0
    markers = sorted({*_PRICE_UPPER, *_PRICE_LOWER, *_PRICE_EXACT}, key=len, reverse=True)
    marker_group = "|".join(re.escape(m) for m in markers)
    # Number immediately followed by its own marker, which is the only shape that states a
    # second condition once the subject (人均) has been elided from the later clause.
    pattern = rf"(?P<num>{_NUM})\s*(?:元|块|块钱|港币|rmb)?\s*(?:{marker_group})"
    return len({match.start("num") for match in re.finditer(pattern, query)})


def refused_condition(query: str) -> tuple[str, str] | None:
    """A ``(code, message)`` for a condition V1 must refuse, or ``None``.

    Refusing is not the same as failing to parse. An unparsed condition degrades to
    unstructured retrieval over the original text, which for these two shapes would
    *contradict* the user: a text search for 不是日料 matches 日料, and a query stating
    two price bounds would be answered by whichever one the ranking happened to favour.
    Both are the silent broadening this boundary exists to prevent, so the caller stops
    rather than guesses.
    """
    negated = _negated_vocabulary_term(query)
    if negated is not None:
        return (
            "unsupported_negated_condition",
            f"V1 无法把否定条件（不是{negated}）表达成结构化过滤条件",
        )
    if _price_condition_count(query) > 1:
        return (
            "conflicting_price_conditions",
            "问题里有多个价格条件，V1 不支持同一字段的组合条件",
        )
    return None


# ------------------------------------------------------- location and cuisine


def _longest_alias_match(query: str, aliases: dict[str, str]) -> str | None:
    """Canonical value for the longest alias occurring in `query`.

    Longest-first is load-bearing: ``油尖旺`` contains ``旺`` and a short-first scan
    over district aliases would match the wrong neighbourhood.
    """
    folded = query.casefold()
    best: tuple[int, str] | None = None
    for alias, canonical in aliases.items():
        if alias in folded and (best is None or len(alias) > best[0]):
            best = (len(alias), canonical)
    return best[1] if best else None


_INTENT_RECOMMEND = ("推荐", "有什么好", "值得去", "该去哪")
_INTENT_EXPLAIN = ("为什么", "怎么样", "什么意思", "解释")

#: Words that suggest the user wants a place, not a dish or a person.
_PLACE_CUES = ("店", "餐厅", "馆", "家", "吃", "料", "菜", "食", "饭")

_SEMANTIC_CUES = (
    "便宜", "性价比", "适合约会", "适合聚餐", "安静", "环境好", "排队", "人少",
    "好吃", "地道", "网红",
)

#: Phrases that map onto a state the product actually writes, scanned in order. Every
#: 想去 variant maps to the same state, so overlap between them is harmless; the order is
#: only load-bearing across *different* states. Negation (不想去) is not handled and
#: deliberately so -- a `present=False` filter needs a phrase the parser can be confident
#: about, and guessing it would produce the exact failure this pass exists to remove: a
#: plan asserting a filter the user did not ask for.
_USER_STATE_CUES: tuple[tuple[str, str], ...] = (
    ("想去", "want_to_go"),
    ("想试", "want_to_try"),
    ("想学", "want_to_learn"),
)


def propose_plan_deterministic(query: str, *, limit: int = 10) -> dict[str, Any]:
    """Build a proposed plan dict from cue matching alone. Never raises.

    Returns a *proposal*, not a plan: the caller still runs it through
    :func:`validate_plan`, so this function is free to be permissive.
    """
    text = (query or "").strip()
    proposal: dict[str, Any] = {"parser": "deterministic", "limit": limit}
    constraints: list[dict[str, Any]] = []

    if any(cue in text for cue in _INTENT_RECOMMEND):
        proposal["intent"] = "recommend"
    elif any(cue in text for cue in _INTENT_EXPLAIN):
        proposal["intent"] = "explain"
    else:
        proposal["intent"] = "find"

    district = _longest_alias_match(text, DISTRICT_ALIASES)
    if district:
        proposal["location"] = {"district": district}

    cuisine = _longest_alias_match(text, CUISINE_ALIASES)
    if cuisine:
        constraints.append({"field": "cuisine", "operator": "=", "value": cuisine})

    price = _price_constraint(text)
    if price:
        constraints.append(price)

    if constraints:
        proposal["claim_constraints"] = constraints

    # A cuisine or district question is about a place, and `entity_types` is enforced, so
    # this is what keeps a *dish* entity named 日式定食 -- which can legitimately carry
    # both a cuisine and a price claim -- out of a restaurant result list.
    #
    # `entity_subtypes` is deliberately not proposed. `subtype = restaurant` is the right
    # description of what the user wants, and nothing in the pipeline writes `subtype`, so
    # asking for it would reject every restaurant in the corpus. The executor enforces the
    # field when a plan carries it; the parser does not manufacture one. Inventing a
    # subtype classifier to close the gap is explicitly out of scope (DEC-020).
    if district or cuisine or any(cue in text for cue in _PLACE_CUES):
        proposal["entity_types"] = ["place"]

    semantic = [cue for cue in _SEMANTIC_CUES if cue in text]
    if semantic:
        proposal["semantic_requirements"] = semantic

    # Only states the product can write are parsed. 去过/没去过 is not among them: see
    # `USER_STATES` and `resurface.INTENT_STATES`. A query saying 去过 therefore keeps its
    # other constraints and simply carries no user-state filter, which is honest -- the
    # alternative is a plan that claims to filter on history and does not.
    for phrase, state in _USER_STATE_CUES:
        if phrase in text:
            proposal["user_state"] = {"state": state, "present": True}
            break

    return proposal


# ------------------------------------------------------------- model assisted

#: Flat JSON schema. Flat because ``MockStructuredModel`` and several real providers
#: handle nesting inconsistently, and because a flat shape is trivially validatable.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["find", "recommend", "explain"]},
        "district": {"type": "string", "description": "行政区名称，例如 旺角"},
        "cuisine": {"type": "string", "description": "菜系，例如 日料"},
        "price_operator": {"type": "string", "enum": ["<", "<=", ">", ">=", "="]},
        "price_value": {"type": "number", "description": "人均价格阈值"},
        "semantic_requirements": {"type": "array", "items": {"type": "string"}},
    },
}

_PARSER_PROMPT = """把用户的问题解析成结构化查询条件。

只输出问题里明确出现的条件。没有出现的字段直接省略，不要猜测、不要补默认值。

字段说明：
- intent: find(找东西) / recommend(要推荐) / explain(问解释)
- district: 行政区名，只有问题里提到才填
- cuisine: 菜系，只有问题里提到才填
- price_operator / price_value: 人均价格约束，例如"人均100以下" -> "<" 和 100
- semantic_requirements: 无法变成硬条件的模糊要求，例如"适合约会"

用户问题：{query}"""


def _propose_plan_with_model(
    query: str, model: StructuredModel, *, limit: int
) -> dict[str, Any] | None:
    """Ask a structured model for a proposal. Returns ``None`` on any failure.

    Degrading silently is correct here: the deterministic proposal is already in hand,
    and a parser outage must not turn into a failed request.
    """
    try:
        response = model.extract(
            _PARSER_PROMPT.format(query=query), PLAN_SCHEMA, temperature=0.0
        )
    except Exception as exc:  # noqa: BLE001 - parser is best-effort by design
        logger.warning("query_plan_model_failed", extra={"error": str(exc)})
        return None

    data = response.data if isinstance(response.data, dict) else {}
    if not data:
        return None

    proposal: dict[str, Any] = {"parser": "model", "limit": limit}
    intent = data.get("intent")
    if isinstance(intent, str):
        proposal["intent"] = intent

    constraints: list[dict[str, Any]] = []
    # Raw values, not canonicalized ones. Canonicalizing here and dropping whatever came
    # back `None` made the proposal divisible: a model answering
    # {"district": "九龙城", "cuisine": "日料"} lost the district it could not resolve and
    # executed the cuisine alone, so a question about 九龙城 was answered with 旺角
    # restaurants -- a different query than the user asked, presented as if it were theirs.
    # `validate_plan` already rejects an out-of-vocabulary value with `unknown_district`;
    # letting the raw string reach it makes the proposal succeed or fail as one unit.
    district_raw = data.get("district") if isinstance(data.get("district"), str) else None
    if district_raw:
        proposal["location"] = {"district": district_raw}

    cuisine_raw = data.get("cuisine") if isinstance(data.get("cuisine"), str) else None
    if cuisine_raw:
        constraints.append({"field": "cuisine", "operator": "=", "value": cuisine_raw})

    operator = data.get("price_operator")
    value = data.get("price_value")
    if isinstance(operator, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
        constraints.append(
            {"field": "price_per_person", "operator": operator, "value": float(value)}
        )

    if constraints:
        proposal["claim_constraints"] = constraints
    semantic = data.get("semantic_requirements")
    if isinstance(semantic, list):
        proposal["semantic_requirements"] = [s for s in semantic if isinstance(s, str)]

    if district_raw or cuisine_raw:
        proposal["entity_types"] = ["place"]
    return proposal


def parse_query(
    query: str,
    *,
    scope: str,
    limit: int = 10,
    structured_model: StructuredModel | None = None,
    conversation_entity_refs: list[str] | None = None,
) -> tuple[QueryPlan, dict[str, Any]]:
    """Parse `query` into a validated plan, plus diagnostics explaining how.

    Always returns a plan. A query with no recognizable constraint yields a valid but
    unstructured plan (``is_structured`` is ``False``), which the caller routes to plain
    hybrid retrieval. Validation failures are recorded in the diagnostics rather than
    raised, because a malformed *model* proposal must not fail the user's request --
    but note that :func:`validate_plan` itself still raises, so a caller constructing a
    plan from an API payload gets the strict behaviour.
    """
    diagnostics: dict[str, Any] = {"parser_attempts": []}
    refs = tuple(conversation_entity_refs or ())

    # Checked before any proposal is built, and recorded as a *refusal* rather than a
    # plan error. A plan error degrades to unstructured retrieval over the raw query,
    # which for these shapes answers the opposite of the question: text search for
    # 不是日料的旺角餐厅 matches 日料. The caller reads `refused` and declines instead.
    refusal = refused_condition(query)
    if refusal is not None:
        code, message = refusal
        logger.info("query_plan_refused", extra={"code": code})
        diagnostics["refused"] = {"code": code, "message": message}
        plan = validate_plan(
            {"parser": "refused", "limit": limit, "conversation_entity_refs": list(refs)},
            raw_query=query,
            scope=scope,
        )
        diagnostics["plan"] = plan.as_dict()
        diagnostics["supported_fields"] = sorted(CLAIM_FIELDS)
        return plan, diagnostics

    proposal = propose_plan_deterministic(query, limit=limit)
    diagnostics["parser_attempts"].append("deterministic")
    deterministic_found = bool(proposal.get("location") or proposal.get("claim_constraints"))

    if not deterministic_found and structured_model is not None:
        model_proposal = _propose_plan_with_model(query, structured_model, limit=limit)
        diagnostics["parser_attempts"].append("model")
        if model_proposal and (
            model_proposal.get("location") or model_proposal.get("claim_constraints")
        ):
            proposal = model_proposal

    if refs:
        proposal["conversation_entity_refs"] = list(refs)

    try:
        plan = validate_plan(proposal, raw_query=query, scope=scope)
    except QueryPlanError as exc:
        # The proposal came from our own cue matcher or a model; either way the user
        # still gets an answer, via unstructured retrieval.
        # `reason`, not `message`: `logging` reserves `message` on `LogRecord` and raises
        # KeyError if `extra` tries to set it. That made this whole branch -- the one that
        # keeps an invalid proposal from failing the user's request -- crash on the first
        # invalid plan it was written to absorb. No test reached it before, because every
        # proposal the deterministic parser builds is valid by construction and the model
        # path only produced pre-canonicalized values.
        logger.warning(
            "query_plan_invalid", extra={"code": exc.code, "reason": exc.message}
        )
        diagnostics["plan_error"] = {"code": exc.code, "message": exc.message}
        plan = validate_plan(
            {"parser": "fallback", "limit": limit, "conversation_entity_refs": list(refs)},
            raw_query=query,
            scope=scope,
        )

    diagnostics["plan"] = plan.as_dict()
    diagnostics["supported_fields"] = sorted(CLAIM_FIELDS)
    return plan, diagnostics
