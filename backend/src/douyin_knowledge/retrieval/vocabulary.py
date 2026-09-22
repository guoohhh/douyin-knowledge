"""Normalized vocabulary for the Structured Retrieval V1 slice.

Two closed vocabularies, deliberately tiny: administrative districts and cuisines.
They exist because ``旺角``, ``Mong Kok`` and ``mongkok`` are the same filter value
and a naive equality test on raw text answers "no" to two thirds of them.

**This is not an ontology.** There is no hierarchy, no geometry and no inference.
``油尖旺`` is *not* an alias of ``旺角`` -- it is the administrative district that
contains Mong Kok, and pretending they are equal would silently widen the user's
filter to two other neighbourhoods. Containment is real geography and modelling it
properly needs a place hierarchy the V1 slice explicitly does not build
(DATA_SCHEMA KM-010, "no graph DB for V1"). So ``油尖旺`` normalizes to its own
canonical value and simply does not match a ``旺角`` query. That is a documented
limitation, not a bug: under-matching is recoverable by the user rephrasing, while
over-matching silently returns restaurants in the wrong neighbourhood.

Canonical values are the Chinese surface form because that is what the corpus and
the query both use; storing an English canonical form would mean translating on
both the write and the read path for no gain.
"""

from __future__ import annotations

from douyin_knowledge.core.text import normalize_ws

__all__ = [
    "CUISINE_ALIASES",
    "DISTRICT_ALIASES",
    "canonical_cuisine",
    "canonical_district",
    "cuisine_surface_forms",
    "district_surface_forms",
]


#: Alias -> canonical district. Keys are matched after :func:`_key` normalization.
DISTRICT_ALIASES: dict[str, str] = {
    "旺角": "旺角",
    "mongkok": "旺角",
    "mong kok": "旺角",
    "mong-kok": "旺角",
    # Containing district, kept distinct on purpose (see module docstring).
    "油尖旺": "油尖旺",
    "yau tsim mong": "油尖旺",
    "尖沙咀": "尖沙咀",
    "tsim sha tsui": "尖沙咀",
    "tst": "尖沙咀",
    "中环": "中环",
    "central": "中环",
    "铜锣湾": "铜锣湾",
    "铜锣灣": "铜锣湾",
    "causeway bay": "铜锣湾",
    "西环": "西环",
    "sai ying pun": "西环",
    "石塘咀": "石塘咀",
    "shek tong tsui": "石塘咀",
}

#: Alias -> canonical cuisine. ``日料`` is the canonical Chinese shorthand.
CUISINE_ALIASES: dict[str, str] = {
    "日料": "日料",
    "日本料理": "日料",
    "日式": "日料",
    "日式定食": "日料",
    "日餐": "日料",
    "和食": "日料",
    "japanese": "日料",
    "japanese food": "日料",
    "粤菜": "粤菜",
    "广东菜": "粤菜",
    "cantonese": "粤菜",
    "港式": "港式",
    "茶餐厅": "港式",
    "川菜": "川菜",
    "四川菜": "川菜",
    "sichuan": "川菜",
    "火锅": "火锅",
    "hotpot": "火锅",
    "hot pot": "火锅",
    "韩料": "韩料",
    "韩国料理": "韩料",
    "韩式": "韩料",
    "korean": "韩料",
    "西餐": "西餐",
    "意大利菜": "意餐",
    "italian": "意餐",
    "泰菜": "泰菜",
    "泰式": "泰菜",
    "thai": "泰菜",
}


def _key(raw: str) -> str:
    """Normalization shared by both vocabularies: trim, collapse space, casefold.

    Casefolding is safe here because neither vocabulary distinguishes case in any
    language it covers, and it is what makes ``MongKok`` and ``mongkok`` agree.
    """
    return normalize_ws(raw).casefold()


def canonical_district(raw: str | None) -> str | None:
    """Canonical district for `raw`, or ``None`` if it is not in the V1 vocabulary.

    Returning ``None`` rather than the raw string is the point: an unrecognized
    district must fail plan validation loudly instead of becoming a filter value
    that matches nothing and looks like an empty collection.
    """
    if not raw:
        return None
    return DISTRICT_ALIASES.get(_key(raw))


def canonical_cuisine(raw: str | None) -> str | None:
    """Canonical cuisine for `raw`, or ``None`` if outside the V1 vocabulary."""
    if not raw:
        return None
    return CUISINE_ALIASES.get(_key(raw))


def district_surface_forms(canonical: str) -> list[str]:
    """Every alias that maps to `canonical`, longest first.

    Longest-first matters for extraction: scanning for ``旺角`` before ``油尖旺``
    would tag a 油尖旺 mention as Mong Kok, because the shorter string is a
    substring of the longer one.
    """
    forms = [alias for alias, target in DISTRICT_ALIASES.items() if target == canonical]
    return sorted(forms, key=len, reverse=True)


def cuisine_surface_forms(canonical: str) -> list[str]:
    """Every alias that maps to `canonical`, longest first."""
    forms = [alias for alias, target in CUISINE_ALIASES.items() if target == canonical]
    return sorted(forms, key=len, reverse=True)
