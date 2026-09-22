"""Rule-based extraction of entities and claims from Chinese short-video text.

Why this exists
---------------
The AI abstraction lets any capability be backed by a real model or a mock. If
the mock returns empty structures, then demo mode — the mode a new user hits
first, with no API key — produces a knowledge base containing no knowledge, and
every downstream layer looks broken for reasons that have nothing to do with it.

So the offline path implements the *same contract* with rules instead of a
model. These regexes are genuinely useful on the target corpus, because food and
travel explore-videos use a small, highly repetitive phrase inventory:
"人均八十", "招牌是菠萝油", "在港大附近", "营业到凌晨两点". They are not a
general Chinese IE system and are not meant to be one.

Everything here is deterministic, which also makes it the right tool for tests:
extraction assertions become stable instead of depending on model sampling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from douyin_knowledge.core.text import normalize_identity, normalize_ws

# ---------------------------------------------------------------- numerals

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def parse_chinese_number(text: str) -> float | None:
    """Parse Chinese numerals ("八十", "一百二十五") and Arabic digits alike.

    Prices in this corpus are spoken, so ASR yields "人均八十" far more often
    than "人均80". Without this, the most valuable structured field in the whole
    dataset stays unusable as a number and cannot be compared or sorted.
    """
    text = text.strip()
    if not text:
        return None

    arabic = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if arabic:
        return float(text)

    total = 0
    current = 0
    matched = False
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            matched = True
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            # Bare "十" means 10, not 0 * 10 — the leading one is implied.
            current = current or 1
            if unit >= 10000:
                total = (total + current) * unit
                current = 0
            else:
                total += current * unit
                current = 0
            matched = True
        else:
            return None
    return float(total + current) if matched else None


_NUM = r"[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万]+"

# ------------------------------------------------------------------ entities

# Characters that can appear next to a name but are never part of one:
# verbs, pronouns, quantifiers, connectives, discourse particles. Without this
# guard the greedy prefix swallows the whole clause — "又去了一次好运茶餐厅"
# comes back as the entity "又去了一次好运茶餐厅" instead of "好运茶餐厅",
# which then fails to dedupe against the same shop named in another video.
_NOT_NAME = (
    "是的了很也都和跟或在去到有说家第次上下这那每我你他她它们个把被就还又再"
    "最非常推荐吃喝逛玩过着但而因所以对从给让呢吧啊哦嘛得会想要没不别"
)
_NAME = rf"[^\W\d_{_NOT_NAME}]"

_ENTITY_PATTERNS: list[tuple[str, str]] = [
    # Longest suffixes first: "茶餐厅" must win over "厅" inside the same name.
    ("place", rf"({_NAME}{{1,8}}(?:茶餐厅|餐厅|饭店|酒楼|茶楼|咖啡馆|咖啡店|小馆|食堂|大排档))"),
    ("place", rf"({_NAME}{{1,8}}(?:公园|寺|神社|车站|机场|大学|学院|医院|市场|商场))"),
    ("place", r"(港大|香港大学|HKU|京都|大阪|东京|上海|北京|深圳|广州)"),
    # Same guard, so "招牌是菠萝油" yields the dish 菠萝油 rather than the
    # whole predicate phrase.
    ("dish", rf"({_NAME}{{1,6}}(?:面|饭|粉|包|饺|汤|粥|茶|咖啡|奶茶|蛋糕|吐司|油))"),
    ("tool", r"\b(MCP|mcp|Claude|ChatGPT|Cursor|Ruff|ruff|Python|TypeScript|React|SQLite)\b"),
]

# Words that match the shape of a name but never *are* one.
_ENTITY_STOPWORDS = {
    "这家店", "那家店", "一家店", "每家店", "很多店", "这家餐厅", "那家餐厅",
    "的面", "的饭", "的茶", "点茶", "喝茶", "吃饭", "米饭", "白饭",
    # 面 as "side/aspect", not noodles.
    "里面", "外面", "上面", "下面", "前面", "后面", "对面", "方面", "表面",
    "一面", "见面", "场面", "画面", "全面", "局面", "水面", "地面", "海面",
    # 油 / 汤 / 茶 as generic mass nouns.
    "酱油", "香油", "花生油", "汤汁", "红茶", "绿茶", "热茶", "冰茶",
}


@dataclass
class MentionCandidate:
    text: str
    entity_type: str
    start: int
    end: int
    context: str

    @property
    def normalized(self) -> str:
        return normalize_identity(self.text)


def extract_mentions(text: str, *, context_window: int = 40) -> list[MentionCandidate]:
    """Find entity mentions, preferring the longest match at each position."""
    text = normalize_ws(text)
    if not text:
        return []

    spans: list[MentionCandidate] = []
    for entity_type, pattern in _ENTITY_PATTERNS:
        for match in re.finditer(pattern, text):
            surface = match.group(1).strip()
            if len(surface) < 2 or surface in _ENTITY_STOPWORDS:
                continue
            start, end = match.span(1)
            spans.append(
                MentionCandidate(
                    text=surface,
                    entity_type=entity_type,
                    start=start,
                    end=end,
                    context=text[max(0, start - context_window) : end + context_window],
                )
            )

    # Drop spans contained in a longer span: "好运茶餐厅" beats "茶餐厅".
    spans.sort(key=lambda s: (s.start, -(s.end - s.start)))
    kept: list[MentionCandidate] = []
    for span in spans:
        if any(k.start <= span.start and span.end <= k.end for k in kept):
            continue
        kept.append(span)

    # Deduplicate by normalized identity, keeping first occurrence.
    seen: set[str] = set()
    unique: list[MentionCandidate] = []
    for span in kept:
        key = f"{span.entity_type}:{span.normalized}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(span)
    return unique


# -------------------------------------------------------------------- claims


@dataclass
class ClaimCandidate:
    """A single extracted assertion, shaped for the `claims` table."""

    predicate: str
    value_type: str
    subject_text: str | None = None
    value_text: str | None = None
    value_number: float | None = None
    value_json: dict[str, Any] | None = None
    unit: str | None = None
    currency: str | None = None
    claim_kind: str = "attribute"
    provenance_type: str = "creator_statement"
    attribution: str | None = None
    confidence: float = 0.6
    evidence_span: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate,
            "value_type": self.value_type,
            "subject_text": self.subject_text,
            "value_text": self.value_text,
            "value_number": self.value_number,
            "value_json": self.value_json,
            "unit": self.unit,
            "currency": self.currency,
            "claim_kind": self.claim_kind,
            "provenance_type": self.provenance_type,
            "attribution": self.attribution,
            "confidence": self.confidence,
            "evidence_span": self.evidence_span,
        }


# Hedges mark a claim as the creator's opinion rather than an observable fact,
# which is what lets the wiki separate "人均 80" from "我觉得很好吃".
_SUBJECTIVE_MARKERS = ("我觉得", "我认为", "个人觉得", "感觉", "应该", "可能", "大概", "似乎")
_RECOMMEND_MARKERS = ("推荐", "必吃", "值得", "一定要", "别错过", "首选")
_NEGATIVE_MARKERS = ("不推荐", "别去", "踩雷", "难吃", "失望", "不值")


def _confidence_for(sentence: str, base: float) -> tuple[float, str]:
    if any(marker in sentence for marker in _SUBJECTIVE_MARKERS):
        return (max(0.35, base - 0.2), "creator_opinion")
    return (base, "creator_statement")


def extract_claims(text: str, *, subject_hint: str | None = None) -> list[ClaimCandidate]:
    """Extract price, recommendation, location and hours claims from text."""
    text = normalize_ws(text)
    if not text:
        return []

    claims: list[ClaimCandidate] = []
    # Sentence-level so a hedge in one clause does not weaken a fact in another.
    sentences = [s for s in re.split(r"[。！？!?；;\n]+", text) if s.strip()]

    for sentence in sentences:
        mentions = extract_mentions(sentence)
        local_subject = next(
            (m.text for m in mentions if m.entity_type == "place"), subject_hint
        )

        for match in re.finditer(rf"人均\s*(?:大概|大约|约)?\s*({_NUM})\s*(?:元|块|块钱|rmb|港币)?", sentence):
            amount = parse_chinese_number(match.group(1))
            if amount is None:
                continue
            confidence, provenance = _confidence_for(sentence, 0.85)
            claims.append(
                ClaimCandidate(
                    predicate="price_per_person",
                    value_type="number",
                    subject_text=local_subject,
                    value_number=amount,
                    value_text=match.group(0),
                    unit="per_person",
                    currency="CNY",
                    claim_kind="measurement",
                    provenance_type=provenance,
                    confidence=confidence,
                    evidence_span=sentence,
                )
            )

        for match in re.finditer(
            rf"(?:营业|开)到\s*(凌晨|早上|晚上|下午)?\s*({_NUM})\s*点", sentence
        ):
            hour = parse_chinese_number(match.group(2))
            if hour is None:
                continue
            claims.append(
                ClaimCandidate(
                    predicate="closing_hour",
                    value_type="number",
                    subject_text=local_subject,
                    value_number=hour,
                    value_text=match.group(0),
                    unit="hour",
                    claim_kind="attribute",
                    confidence=0.7,
                    evidence_span=sentence,
                )
            )

        for match in re.finditer(
            r"(?:招牌|必点|主打|特色)(?:是|菜是|为)?\s*([一-鿿]{2,10})", sentence
        ):
            claims.append(
                ClaimCandidate(
                    predicate="signature_item",
                    value_type="text",
                    subject_text=local_subject,
                    value_text=match.group(1),
                    claim_kind="attribute",
                    confidence=0.65,
                    evidence_span=sentence,
                )
            )

        if any(marker in sentence for marker in _RECOMMEND_MARKERS):
            confidence, provenance = _confidence_for(sentence, 0.6)
            claims.append(
                ClaimCandidate(
                    predicate="recommended",
                    value_type="boolean",
                    subject_text=local_subject,
                    value_text="true",
                    value_json={"polarity": "positive"},
                    claim_kind="evaluation",
                    provenance_type=provenance,
                    confidence=confidence,
                    evidence_span=sentence,
                )
            )
        if any(marker in sentence for marker in _NEGATIVE_MARKERS):
            confidence, provenance = _confidence_for(sentence, 0.6)
            claims.append(
                ClaimCandidate(
                    predicate="recommended",
                    value_type="boolean",
                    subject_text=local_subject,
                    value_text="false",
                    value_json={"polarity": "negative"},
                    claim_kind="evaluation",
                    provenance_type=provenance,
                    confidence=confidence,
                    evidence_span=sentence,
                )
            )

        for match in re.finditer(
            r"(?:在|位于)\s*([一-鿿]{2,10}?)\s*(?:附近|旁边|对面|周边|一带)", sentence
        ):
            claims.append(
                ClaimCandidate(
                    predicate="near",
                    value_type="text",
                    subject_text=local_subject,
                    value_text=match.group(1),
                    claim_kind="relation",
                    confidence=0.7,
                    evidence_span=sentence,
                )
            )

        claims.extend(_district_claims(sentence, local_subject))
        claims.extend(_cuisine_claims(sentence, local_subject))

    return _dedupe_claims(claims)


def _district_claims(sentence: str, subject: str | None) -> list[ClaimCandidate]:
    """Emit a ``located_in`` claim when a known district is named.

    Values are canonical, not the matched surface form, so ``Mong Kok`` and ``旺角``
    produce the same filterable value. Normalizing on write is what lets the structured
    executor use exact equality instead of fuzzy matching at query time.

    The vocabulary is closed on purpose (see :mod:`douyin_knowledge.retrieval.vocabulary`).
    An unknown place name produces no claim rather than a ``located_in`` claim the
    executor can never match, because an unmatchable claim looks like knowledge in the
    wiki while being useless for retrieval.
    """
    from douyin_knowledge.retrieval.vocabulary import DISTRICT_ALIASES

    folded = sentence.casefold()
    hits = [
        (len(alias), alias, canonical)
        for alias, canonical in DISTRICT_ALIASES.items()
        if alias in folded
    ]
    if not hits:
        return []
    # Longest alias wins: 油尖旺 contains 旺 and a shorter match would file a Mong Kok
    # restaurant under the wrong district.
    _, alias, canonical = max(hits, key=lambda item: item[0])
    return [
        ClaimCandidate(
            predicate="located_in",
            value_type="text",
            subject_text=subject,
            value_text=canonical,
            claim_kind="attribute",
            provenance_type="creator_statement",
            confidence=0.75,
            # The span is the whole sentence, not the alias: grounding validates that some
            # surface form of the canonical value appears here, and the alias alone would
            # make that check trivially true.
            evidence_span=sentence,
        )
    ]


def _cuisine_claims(sentence: str, subject: str | None) -> list[ClaimCandidate]:
    """Emit a ``cuisine`` claim when a known cuisine is named. Canonical values only."""
    from douyin_knowledge.retrieval.vocabulary import CUISINE_ALIASES

    folded = sentence.casefold()
    hits = [
        (len(alias), canonical)
        for alias, canonical in CUISINE_ALIASES.items()
        if alias in folded
    ]
    if not hits:
        return []
    _, canonical = max(hits, key=lambda item: item[0])
    return [
        ClaimCandidate(
            predicate="cuisine",
            value_type="text",
            subject_text=subject,
            value_text=canonical,
            claim_kind="attribute",
            provenance_type="creator_statement",
            confidence=0.7,
            evidence_span=sentence,
        )
    ]


def _dedupe_claims(claims: list[ClaimCandidate]) -> list[ClaimCandidate]:
    """Keep the highest-confidence claim per (subject, predicate, value)."""
    best: dict[tuple[str, str, str], ClaimCandidate] = {}
    for claim in claims:
        key = (
            normalize_identity(claim.subject_text or ""),
            claim.predicate,
            claim.value_text or str(claim.value_number),
        )
        current = best.get(key)
        if current is None or claim.confidence > current.confidence:
            best[key] = claim
    return list(best.values())


__all__ = [
    "ClaimCandidate",
    "MentionCandidate",
    "extract_claims",
    "extract_mentions",
    "parse_chinese_number",
]
