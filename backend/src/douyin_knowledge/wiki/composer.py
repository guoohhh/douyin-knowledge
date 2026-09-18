"""Deterministic page composition: Claims -> markdown statements.

The composer is the reason the Wiki works with no API key at all. It turns the
structured claim set for one subject into an ordered list of ``Statement``
objects, each carrying the claim ids that justify it. ``WikiSupport`` rows are
written from those ids, which is how WIKI-004 (statement-level provenance) is
satisfied without trusting a model to report its own sources.

Two properties matter more than prose quality:

1. **Stable statement keys.** A key is derived from the predicate, not from a
   line number, so support rows survive re-composition when an unrelated
   statement is added above. Keying on position would silently reattach
   citations to the wrong sentence on the next revision.
2. **No unsourced facts.** Every factual line is generated *from* claims. The
   composer cannot invent a value it was not given, so a page can never state
   something the evidence spine does not contain.

An LLM may later rewrite the prose (see ``integrator``), but it operates on this
grounded skeleton rather than on raw text, and its output is validated back
against these claim ids.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from douyin_knowledge.core.text import normalize_ws

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from douyin_knowledge.db.models.entities import Claim

PAGE_TYPE_ENTITY = "entity"
PAGE_TYPE_CONCEPT = "concept"
PAGE_TYPE_TOPIC = "topic"
PAGE_TYPE_SYNTHESIS = "synthesis"
PAGE_TYPE_SOURCE_DIGEST = "source_digest"

PAGE_TYPES = (
    PAGE_TYPE_ENTITY,
    PAGE_TYPE_CONCEPT,
    PAGE_TYPE_TOPIC,
    PAGE_TYPE_SYNTHESIS,
    PAGE_TYPE_SOURCE_DIGEST,
)

# Predicates rendered with a dedicated sentence, in display order. Anything not
# listed still appears, under a generic "其他" grouping — an unknown predicate
# must never cause knowledge to be dropped silently.
_PREDICATE_LABELS: dict[str, str] = {
    "price_per_person": "人均价格",
    "signature_item": "招牌 / 必点",
    "recommended": "推荐度",
    "not_recommended": "不推荐",
    "closing_hour": "营业时间",
    "opening_hour": "营业时间",
    "near": "位置",
    "located_in": "位置",
    "address": "地址",
    "queue_time": "排队时间",
    "rating": "评分",
    "cuisine": "菜系",
    "definition": "定义",
    "purpose": "用途",
    "comparison": "对比",
    "requirement": "前置条件",
}

_PREDICATE_ORDER = list(_PREDICATE_LABELS)

# An opinion is presented as an opinion. Flattening "我觉得很好吃" into "很好吃"
# is the single most common way a knowledge system starts lying to its user.
_OPINION_PROVENANCE = {"creator_opinion", "third_party"}


@dataclass
class Statement:
    """One material line of a wiki page plus the claims that justify it."""

    key: str
    text: str
    claim_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    conflicting: bool = False

    def as_markdown(self) -> str:
        marker = " ⚠️ 存在分歧" if self.conflicting else ""
        return f"- {self.text}{marker}"


@dataclass
class ComposedPage:
    """The full deterministic composition result for one subject."""

    title: str
    page_type: str
    summary: str
    statements: list[Statement]
    frontmatter: dict[str, Any]

    @property
    def content_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        if self.summary:
            lines += [self.summary, ""]
        if self.statements:
            lines += ["## 已知信息", ""]
            lines += [s.as_markdown() for s in self.statements]
            lines.append("")
        counts = self.frontmatter.get("source_count")
        if counts:
            lines += [
                "## 出处",
                "",
                f"本页由 {counts} 个来源的 {self.frontmatter.get('claim_count', 0)} 条陈述编译而成。",
                "每条信息都可以追回到具体视频与时间点。",
                "",
            ]
        return "\n".join(lines).rstrip() + "\n"


def render_claim_value(claim: Claim) -> str:
    """Render a claim's value the way a reader expects to see it.

    Shared with the answer generator so a price reads identically on a wiki page
    and in a chat answer; two renderers would eventually disagree and look like
    two different facts.
    """
    if claim.value_number is not None:
        number = claim.value_number
        rendered = f"{number:g}"
        if claim.predicate in {"closing_hour", "opening_hour"}:
            hour = int(number) % 24
            return f"{hour} 点"
        unit = claim.unit or ""
        currency = claim.currency or ""
        if currency:
            return f"{rendered} {currency}".strip()
        return f"{rendered}{unit}".strip()
    if claim.value_text:
        return normalize_ws(claim.value_text)
    if isinstance(claim.value_json, dict):
        label = claim.value_json.get("label") or claim.value_json.get("text")
        if label:
            return normalize_ws(str(label))
    if claim.value_type == "boolean":
        return "是" if claim.value_text in {"true", "1", "yes"} else "否"
    return ""


def _attribution_phrase(claim: Claim) -> str:
    who = normalize_ws(claim.attribution or "")
    if not who or who == "unknown_creator":
        who = "来源作者"
    return who


def _group_claims(claims: Sequence[Claim]) -> dict[str, list[Claim]]:
    grouped: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        grouped[claim.predicate].append(claim)
    return grouped


def _distinct_values(claims: Sequence[Claim]) -> list[str]:
    seen: list[str] = []
    for claim in claims:
        value = render_claim_value(claim)
        if value and value not in seen:
            seen.append(value)
    return seen


def _compose_statement(predicate: str, claims: Sequence[Claim]) -> Statement | None:
    """Build one statement from every claim sharing a predicate.

    Multiple claims for the same predicate are the interesting case: two videos
    quoting different prices is knowledge, not an error, so both values are kept
    and the statement is flagged rather than one being silently dropped.
    """
    label = _PREDICATE_LABELS.get(predicate, predicate)
    values = _distinct_values(claims)
    claim_ids = [c.id for c in claims]
    source_ids = list(dict.fromkeys(c.source_id for c in claims))

    if predicate in {"recommended", "not_recommended"} and not values:
        # A bare evaluation carries no value column; the predicate *is* the
        # information, so render it from attribution instead of skipping.
        voices = list(dict.fromkeys(_attribution_phrase(c) for c in claims))
        verb = "推荐" if predicate == "recommended" else "不推荐"
        text = f"{label}：{'、'.join(voices)} {verb}"
        return Statement(key=f"fact:{predicate}", text=text, claim_ids=claim_ids,
                         source_ids=source_ids)

    if not values:
        return None

    opinion_only = all(c.provenance_type in _OPINION_PROVENANCE for c in claims)
    if len(values) == 1:
        body = values[0]
        conflicting = False
    else:
        body = " / ".join(values)
        conflicting = True

    if opinion_only:
        voices = list(dict.fromkeys(_attribution_phrase(c) for c in claims))
        text = f"{label}：{body}（{'、'.join(voices)}的说法）"
    else:
        text = f"{label}：{body}"

    return Statement(
        key=f"fact:{predicate}",
        text=text,
        claim_ids=claim_ids,
        source_ids=source_ids,
        conflicting=conflicting,
    )


def compose_page(
    *,
    title: str,
    page_type: str,
    claims: Sequence[Claim],
    aliases: Sequence[str] = (),
    extra_frontmatter: dict[str, Any] | None = None,
) -> ComposedPage:
    """Compose a wiki page deterministically from a claim set."""
    grouped = _group_claims(claims)
    ordered_predicates = [p for p in _PREDICATE_ORDER if p in grouped]
    ordered_predicates += sorted(p for p in grouped if p not in _PREDICATE_LABELS)

    statements: list[Statement] = []
    for predicate in ordered_predicates:
        statement = _compose_statement(predicate, grouped[predicate])
        if statement is not None:
            statements.append(statement)

    source_ids = sorted({c.source_id for c in claims})
    summary = _compose_summary(title, statements, len(source_ids))

    frontmatter: dict[str, Any] = {
        "page_type": page_type,
        "aliases": list(dict.fromkeys(aliases)),
        "claim_count": len(claims),
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "predicates": ordered_predicates,
        "composer": "deterministic-1",
    }
    if extra_frontmatter:
        frontmatter.update(extra_frontmatter)

    return ComposedPage(
        title=title,
        page_type=page_type,
        summary=summary,
        statements=statements,
        frontmatter=frontmatter,
    )


def _compose_summary(title: str, statements: Sequence[Statement], source_count: int) -> str:
    if not statements:
        return f"关于「{title}」目前还没有可引用的结构化信息。"
    lead = statements[0].text
    if source_count > 1:
        return f"「{title}」出现在 {source_count} 个收藏来源中。{lead}。"
    return f"「{title}」来自 1 个收藏来源。{lead}。"
