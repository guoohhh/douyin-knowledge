"""The grounding contract a model answer must satisfy before it may be returned.

The previous check asked one question -- "does every ``[n]`` the model emitted refer to a
citation we supplied?" -- and repaired a failure by deleting the offending marker. Two
real Stage 3C answers walked straight through it.

*A bracket that is not a citation.* The model wrote ``[第五步]``. The old regex matched
``\\[(\\d+)\\]`` only, so a non-numeric bracket was not a marker to be validated, it was
ordinary text. It rendered exactly like provenance and pointed at nothing.

*An assertion with no bracket at all.* Another answer carried several substantive factual
sentences with no marker anywhere. Every marker it *did* emit was valid, so the check
passed and the answer was returned as a normal cited answer. "Every emitted number is a
real ordinal" and "every assertion has support" are different properties, and only the
second one is the contract.

So validation here is *whole-answer* and its result is a verdict, not a repaired string.
Stripping a bad marker is the wrong remedy in any case: deleting ``[99]`` from
"还有分店[99]" leaves "还有分店", which is the same unsupported assertion with its
falseness now invisible. When the contract is violated the answer is not patched, it is
**refused** -- the caller falls back to material composed from claims and evidence, which
cannot be ungrounded because no model wrote it.

What a sentence must do to need support is deliberately inverted from the intuitive
direction: *every* sentence needs a citation unless it is structurally incapable of
asserting anything about the collection (a heading, a bullet skeleton) or it is framed
from its first character as a statement about the evidence rather than about the world
(不确定…, 证据里没有提到…). Whitelisting "what looks factual" would make the contract only
as good as a keyword list, and the failure mode of a missed keyword is an unsupported
claim presented as grounded. Inverted, a missed case costs a fallback instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Any ASCII bracket group. Full-width 【】 are deliberately *not* matched: the protocol in
#: the system prompt is ``[n]``, and 【】 is ordinary Chinese punctuation used for emphasis
#: and headings. Treating it as a malformed citation would fail valid answers.
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")

#: A well-formed citation body: one ordinal, or several separated by , ，、 or space.
#: ``[1]``, ``[1,2]`` and ``[1、2]`` are all real citation syntax a model reaches for.
_CITATION_BODY_RE = re.compile(r"^\s*\d+(\s*[,，、]\s*\d+)*\s*$")

_ORDINAL_RE = re.compile(r"\d+")

#: Sentence boundaries. Commas are *not* boundaries -- a clause inside one sentence often
#: carries the marker for the whole sentence ("人均八十[1]，环境也安静"), and splitting on
#: commas would report the second clause as uncited when its support sits in the first.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|\n+")

#: A markdown heading, or a bullet/number skeleton with no prose in it. Structure, not
#: assertion, so it has nothing to support.
_STRUCTURAL_RE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s*$|\d+[.、)]\s*$|[-*+]\s*#{1,6}\s)")

#: Characters that carry no assertion on their own.
_PUNCT_RE = re.compile(r"[\s。，、；：！？…—\-·（）()\[\]「」『』“”\"'*#>|]+")

#: Sentence-initial frames that make a sentence a statement *about the evidence* rather
#: than about the world. Anchored at position 0 on purpose: 证据里没有提到营业时间 is a
#: report on the archive and needs no citation, while 这家店很好吃，不过证据不足 bolts the
#: frame onto an assertion that does. Anchoring is what stops the exemption from becoming
#: a way to smuggle an uncited claim past the contract.
#:
#: This list exists because the system prompt *requires* these sentences ("不确定就说不确定。
#: 证据不足就直说证据不足"), so a contract that rejected them would contradict the
#: instruction the model was given.
_META_FRAMES = (
    "不确定",
    "无法确定",
    "不清楚",
    "没有把握",
    "证据不足",
    "证据里没有",
    "证据中没有",
    "证据里未",
    "证据没有",
    "现有证据",
    "提供的证据",
    "上面的证据",
    "根据现有",
    "我没有找到",
    "没有找到",
    "找不到",
    "无法回答",
    "以下是",
    "以上是",
    "综上",
    "注意：",
    "说明：",
    "补充：",
    "需要你自己判断",
    "建议你",
    "你可以",
    "希望",
)

#: Below this many content characters a fragment cannot be a factual claim. Two, not more:
#: 很好吃 is three characters and is absolutely an assertion.
_MIN_CONTENT_CHARS = 2

_DETAIL_LIMIT = 80


@dataclass(frozen=True)
class GroundingViolation:
    """One way an answer broke the contract, with the text that broke it."""

    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


def _content_chars(text: str) -> str:
    return _PUNCT_RE.sub("", _BRACKET_RE.sub("", text))


def _needs_support(sentence: str) -> bool:
    """Whether this sentence asserts something that requires a citation."""
    stripped = sentence.strip()
    if not stripped:
        return False
    if _STRUCTURAL_RE.match(stripped):
        return False
    # The frame check runs on the heading-free, marker-free head of the sentence so that
    # "- 不确定营业时间" and "**证据不足**" are recognised as frames rather than treated as
    # prose that happens to start with punctuation.
    head = stripped.lstrip("-*+>#0123456789.、) \t")
    if head.startswith(_META_FRAMES):
        return False
    return len(_content_chars(stripped)) >= _MIN_CONTENT_CHARS


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _DETAIL_LIMIT:
        return collapsed
    return collapsed[:_DETAIL_LIMIT] + "…"


def validate_grounding(text: str, valid_ordinals: set[int]) -> list[GroundingViolation]:
    """Every way ``text`` fails the grounding contract. Empty list means it passes.

    Three checks, in the order a reader would apply them: are the brackets citations at
    all, do they point at citations we supplied, and does every assertion have one.
    """
    violations: list[GroundingViolation] = []
    seen: set[tuple[str, str]] = set()

    def record(kind: str, detail: str) -> None:
        item = (kind, _truncate(detail))
        if item in seen:
            return
        seen.add(item)
        violations.append(GroundingViolation(kind=item[0], detail=item[1]))

    for match in _BRACKET_RE.finditer(text):
        body = match.group(1)
        if not _CITATION_BODY_RE.match(body):
            # `[第五步]`, `[来源]`, `[]` -- bracket-shaped text that reads as provenance
            # and carries none.
            record("pseudo_citation", match.group(0))
            continue
        for ordinal in _ORDINAL_RE.findall(body):
            if int(ordinal) not in valid_ordinals:
                record("unknown_ordinal", match.group(0))

    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not _needs_support(sentence):
            continue
        supported = False
        for match in _BRACKET_RE.finditer(sentence):
            body = match.group(1)
            if not _CITATION_BODY_RE.match(body):
                continue
            if any(int(o) in valid_ordinals for o in _ORDINAL_RE.findall(body)):
                supported = True
                break
        if not supported:
            record("uncited_assertion", sentence)

    return violations


def unknown_ordinals(text: str, valid_ordinals: set[int]) -> set[int]:
    """Ordinals cited in ``text`` that we never supplied.

    Kept separate from :func:`validate_grounding` because it answers a narrower,
    quantitative question that the ``stripped_markers`` diagnostic reports.
    """
    found: set[int] = set()
    for match in _BRACKET_RE.finditer(text):
        body = match.group(1)
        if not _CITATION_BODY_RE.match(body):
            continue
        for ordinal in _ORDINAL_RE.findall(body):
            if int(ordinal) not in valid_ordinals:
                found.add(int(ordinal))
    return found


def violations_as_list(violations: list[GroundingViolation]) -> list[dict[str, Any]]:
    return [v.as_dict() for v in violations]


__all__ = [
    "GroundingViolation",
    "unknown_ordinals",
    "validate_grounding",
    "violations_as_list",
]
