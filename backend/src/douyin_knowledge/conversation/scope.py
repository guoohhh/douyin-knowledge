"""Knowledge-scope classification (RET-002, RET-003).

The single most important trust property of this product is that the user can
always tell what came from their own collection and what came from the model's
general knowledge. That distinction is decided here, before retrieval runs,
because it changes both what we search and what we are allowed to say.

Classification is deterministic keyword matching, not a model call. Three
reasons: the rules are explicit product policy rather than a judgement call, a
misclassification silently breaks the trust boundary, and the user is entitled
to a stable answer for the same phrasing.

Default is ``personal_first`` (RET-003): inside a personal knowledge tool, an
ambiguous question is a question about the user's own saves. A general-chatbot
default would quietly turn external memory into a search engine.
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPE_PERSONAL_REQUIRED = "personal_required"
SCOPE_PERSONAL_FIRST = "personal_first"
SCOPE_GENERAL = "general"
SCOPE_HYBRID = "hybrid"

SCOPES = (SCOPE_PERSONAL_REQUIRED, SCOPE_PERSONAL_FIRST, SCOPE_GENERAL, SCOPE_HYBRID)

_PERSONAL_MARKERS = (
    "我收藏", "我的收藏", "我之前收藏", "我存过", "我之前存", "我保存",
    "收藏里", "收藏夹", "我看过的视频", "我刷到", "我的合集",
    "my collection", "i saved", "i bookmarked",
)

_GENERAL_MARKERS = (
    "一般来说", "通常来说", "不看我的收藏", "不用查我的收藏", "你知道的",
    "普遍", "常识", "in general", "generally speaking",
)

# Hybrid requires an explicit request to combine; otherwise a personal question
# that happens to contain "和" would leak general knowledge into a cited answer.
_HYBRID_MARKERS = (
    "结合我的收藏", "结合我收藏", "加上你的知识", "结合你的知识",
    "除了我的收藏", "再补充一些", "combine my collection",
)


@dataclass(frozen=True)
class ScopeDecision:
    """The scope plus the evidence for it, so the UI can explain itself."""

    scope: str
    matched_marker: str | None
    reason: str

    @property
    def uses_collection(self) -> bool:
        return self.scope != SCOPE_GENERAL

    @property
    def allows_general_knowledge(self) -> bool:
        """Whether the generator may add statements not backed by local evidence."""
        return self.scope in {SCOPE_GENERAL, SCOPE_HYBRID}

    @property
    def requires_evidence(self) -> bool:
        """Whether an empty retrieval must produce a 'no result' answer.

        True for both personal modes: ``personal_first`` may *fall back* to
        broader phrasing but must not invent collection content, and RETRIEVAL.md
        15 requires saying "我没找到" rather than fabricating a match.
        """
        return self.scope in {SCOPE_PERSONAL_REQUIRED, SCOPE_PERSONAL_FIRST}

    def as_dict(self) -> dict[str, str | None]:
        return {"scope": self.scope, "matched_marker": self.matched_marker, "reason": self.reason}


def _first_match(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for marker in markers:
        if marker in text or marker in lowered:
            return marker
    return None


def classify_scope(query: str, *, override: str | None = None) -> ScopeDecision:
    """Decide the knowledge scope for one user turn.

    An explicit ``override`` from the UI wins: the user toggling a scope switch
    is a stronger signal than anything inferable from wording.
    """
    if override:
        if override not in SCOPES:
            raise ValueError(f"unknown knowledge scope {override!r}")
        return ScopeDecision(override, None, "explicit_override")

    text = (query or "").strip()
    if not text:
        return ScopeDecision(SCOPE_PERSONAL_FIRST, None, "empty_query_default")

    # Hybrid is checked first: "结合我的收藏和你的知识" contains a personal
    # marker too, and personal_required would strip the general half away.
    hybrid = _first_match(text, _HYBRID_MARKERS)
    if hybrid:
        return ScopeDecision(SCOPE_HYBRID, hybrid, "explicit_combination_request")

    general = _first_match(text, _GENERAL_MARKERS)
    personal = _first_match(text, _PERSONAL_MARKERS)

    if general and not personal:
        return ScopeDecision(SCOPE_GENERAL, general, "explicit_general_wording")
    if personal:
        return ScopeDecision(SCOPE_PERSONAL_REQUIRED, personal, "explicit_personal_wording")

    return ScopeDecision(SCOPE_PERSONAL_FIRST, None, "ambiguous_defaults_to_personal")


__all__ = [
    "SCOPES",
    "SCOPE_GENERAL",
    "SCOPE_HYBRID",
    "SCOPE_PERSONAL_FIRST",
    "SCOPE_PERSONAL_REQUIRED",
    "ScopeDecision",
    "classify_scope",
]
