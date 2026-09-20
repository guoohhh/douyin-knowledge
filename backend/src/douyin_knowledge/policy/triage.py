"""Cheap content-type triage: what kind of video is this, decided before paying for it.

The product requirement (PROCESSING_POLICY.md 5.4) is to skip entertainment clips --
film cuts, variety show moments, music edits, memes, sports highlights -- without
transcribing them first. That ordering is the whole point: an exclusion that only fires
after ASR has run has already spent the money it was meant to save.

So triage is deliberately *not* a model call by default. It reads the text the provider
already gave us and matches cue phrases. Only when the cues are inconclusive, a semantic
rule actually depends on the answer, and a model is configured, does it escalate to one
cheap structured call. A classifier that always asked a model would reintroduce the cost
it exists to avoid, and under `DK_AI_PROVIDER=mock` it would invent labels from a stub.

These labels are routing hints, not the knowledge taxonomy (PROCESSING_POLICY.md 5.4).
Nothing downstream reasons about `movie_clip` as a fact about the world; it only decides
whether to spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Reusing the package's own StrEnum rather than importing from `enum` directly: CI still
# runs the test suite on 3.10 (`requires-python = ">=3.10"`), so the backport in
# `policy/models.py` is load-bearing, and adding a third copy is how the two existing ones
# drift apart.
#
# The type-checking branch imports the real one instead. mypy runs at `python_version =
# "3.12"` but still resolves the runtime conditional to the backport's `class StrEnum(str,
# Enum)`, which it then treats as a plain class rather than an enum -- so `CUES:
# dict[ContentType, ...]` was rejected because every member typed as `str`. Under 3.12 the
# two are the same object, so this narrows the type without changing behaviour.
if TYPE_CHECKING:
    from enum import StrEnum
else:
    from douyin_knowledge.policy.models import StrEnum

if TYPE_CHECKING:
    from douyin_knowledge.ai.providers import StructuredModel
    from douyin_knowledge.policy.signal import SourceSignal


class ContentType(StrEnum):
    """Routing labels for triage. `UNKNOWN` is the honest default."""

    MOVIE_CLIP = "movie_clip"
    VARIETY_CLIP = "variety_clip"
    MUSIC_CLIP = "music_clip"
    MEME = "meme"
    SPORTS_HIGHLIGHT = "sports_highlight"
    OTHER_ENTERTAINMENT = "other_entertainment"
    KNOWLEDGE = "knowledge"
    UNKNOWN = "unknown"


class TriageMethod(StrEnum):
    """How a classification was reached -- recorded, because it changes how much to trust it."""

    CUES = "cues"
    MODEL = "model"
    NONE = "none"


#: Cue phrases per label, matched case-folded against `SourceSignal.haystack`.
#:
#: Chinese cues come first because that is what this corpus is. The English ones are not
#: translations for their own sake: creators routinely tag bilingually, and a tag is often
#: the only place the genre is stated at all.
CUES: dict[ContentType, tuple[str, ...]] = {
    ContentType.MOVIE_CLIP: (
        "电影片段", "电影剪辑", "影视剪辑", "电影解说", "影视解说", "经典台词",
        "看电影", "movie clip", "film clip", "movieclip",
    ),
    ContentType.VARIETY_CLIP: (
        "综艺", "综艺片段", "综艺剪辑", "综艺笑点", "真人秀", "脱口秀",
        "variety show", "talk show",
    ),
    ContentType.MUSIC_CLIP: (
        "音乐片段", "歌曲剪辑", "翻唱", "mv", "现场演唱", "live版", "原创歌曲",
        "music clip", "cover song",
    ),
    ContentType.MEME: (
        "搞笑", "沙雕", "整活", "鬼畜", "表情包", "梗图", "笑死",
        "meme", "funny clip",
    ),
    ContentType.SPORTS_HIGHLIGHT: (
        "集锦", "进球", "绝杀", "高光时刻", "比赛回放", "赛事集锦",
        "highlights", "full match",
    ),
    ContentType.OTHER_ENTERTAINMENT: (
        "追星", "饭圈", "娱乐八卦", "明星", "cut合集",
        "fancam", "celebrity",
    ),
    ContentType.KNOWLEDGE: (
        "教程", "教学", "科普", "攻略", "干货", "测评", "评测", "推荐", "分享",
        "如何", "怎么做", "避坑", "笔记",
        "tutorial", "how to", "guide", "review",
    ),
}

#: An entertainment label needs a stronger showing than a knowledge label before it can
#: cost the user a skipped video. Confidence is not a probability; it is a stated
#: willingness to act, and the asymmetry is the point: wrongly skipping a useful video
#: loses knowledge silently, whereas wrongly processing a film clip only wastes a call.
_BASE_CONFIDENCE = 0.62
_PER_EXTRA_CUE = 0.12
_MAX_CUE_CONFIDENCE = 0.92


@dataclass(frozen=True)
class TriageResult:
    """One classification plus enough context to audit it."""

    content_type: ContentType
    confidence: float
    method: TriageMethod
    cues: tuple[str, ...] = ()
    model_name: str | None = None
    #: Which other labels also matched. Kept because a video tagged both 综艺 and 教程 is a
    #: genuinely ambiguous case, and the UI should be able to say so rather than present
    #: the winner as obvious.
    runners_up: tuple[str, ...] = ()

    @property
    def is_entertainment(self) -> bool:
        return self.content_type in ENTERTAINMENT_TYPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_type": str(self.content_type),
            "confidence": round(self.confidence, 4),
            "method": str(self.method),
            "cues": list(self.cues),
            "model_name": self.model_name,
            "runners_up": list(self.runners_up),
        }


ENTERTAINMENT_TYPES = frozenset(
    {
        ContentType.MOVIE_CLIP,
        ContentType.VARIETY_CLIP,
        ContentType.MUSIC_CLIP,
        ContentType.MEME,
        ContentType.SPORTS_HIGHLIGHT,
        ContentType.OTHER_ENTERTAINMENT,
    }
)

UNKNOWN = TriageResult(
    content_type=ContentType.UNKNOWN,
    confidence=0.0,
    method=TriageMethod.NONE,
)


@dataclass
class CheapTriage:
    """Classify a source from its metadata, escalating to a model only if asked to.

    `model` is optional and unused unless `classify(..., allow_model=True)` and the cues
    came back `UNKNOWN`. Passing a model does not mean it will be called.
    """

    model: StructuredModel | None = None
    model_name: str | None = None
    #: Records every prompt actually sent, so a test can assert *no* model call happened.
    #: "Cheap" is a behavioural claim, and a claim worth asserting.
    model_calls: list[str] = field(default_factory=list)

    def classify(self, signal: SourceSignal, *, allow_model: bool = False) -> TriageResult:
        result = self.classify_by_cues(signal)
        if result.content_type is not ContentType.UNKNOWN:
            return result
        if allow_model and self.model is not None:
            return self.classify_by_model(signal)
        return result

    # -------------------------------------------------------------------- cues

    def classify_by_cues(self, signal: SourceSignal) -> TriageResult:
        haystack = signal.haystack
        if not haystack.strip():
            return UNKNOWN

        hits: dict[ContentType, list[str]] = {}
        for label, cues in CUES.items():
            matched = [cue for cue in cues if cue in haystack]
            if matched:
                hits[label] = matched

        if not hits:
            return UNKNOWN

        # Most cues wins; ties break toward KNOWLEDGE, i.e. toward processing. A video
        # that reads as both a tutorial and a variety clip should be read, not skipped.
        best = max(
            hits.items(),
            key=lambda item: (len(item[1]), item[0] is ContentType.KNOWLEDGE),
        )
        label, matched = best
        confidence = min(
            _MAX_CUE_CONFIDENCE,
            _BASE_CONFIDENCE + _PER_EXTRA_CUE * (len(matched) - 1),
        )
        return TriageResult(
            content_type=label,
            confidence=confidence,
            method=TriageMethod.CUES,
            cues=tuple(matched),
            runners_up=tuple(sorted(str(k) for k in hits if k is not label)),
        )

    # ------------------------------------------------------------------- model

    def classify_by_model(self, signal: SourceSignal) -> TriageResult:
        """One cheap structured call, used only as a fallback.

        A malformed or unrecognised label degrades to `UNKNOWN` rather than raising.
        Triage is an optimisation; a classifier that can fail the whole pipeline is worse
        than one that occasionally says "I don't know" and lets processing proceed.
        """
        if self.model is None:
            return UNKNOWN

        prompt = _PROMPT.format(
            title=signal.title or "(no title)",
            caption=(signal.caption or "")[:600] or "(no caption)",
            hashtags=", ".join(signal.hashtags) or "(none)",
            duration_s=round((signal.duration_ms or 0) / 1000),
        )
        self.model_calls.append(prompt)
        try:
            response = self.model.extract(prompt, _SCHEMA, temperature=0.0)
            data = response.data if isinstance(response.data, dict) else {}
        except Exception:
            return UNKNOWN

        raw_label = str(data.get("content_type") or "").strip()
        try:
            label = ContentType(raw_label)
        except ValueError:
            return UNKNOWN

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        return TriageResult(
            content_type=label,
            confidence=max(0.0, min(1.0, confidence)),
            method=TriageMethod.MODEL,
            model_name=self.model_name,
        )


_PROMPT = """\
Classify this short video by the kind of content it is, for the purpose of deciding
whether it is worth transcribing and extracting knowledge from.

Title: {title}
Caption: {caption}
Hashtags: {hashtags}
Duration: {duration_s}s

Answer `knowledge` if it plausibly contains information worth keeping (a review, a
tutorial, a recommendation, an explanation, a travel or food note). Answer one of the
entertainment labels only if the video is primarily entertainment with little durable
information. Answer `unknown` if the metadata does not support a confident call.
"""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_type": {
            "type": "string",
            "enum": [str(c) for c in ContentType],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["content_type", "confidence"],
}


__all__ = [
    "CUES",
    "ENTERTAINMENT_TYPES",
    "CheapTriage",
    "ContentType",
    "TriageMethod",
    "TriageResult",
]
