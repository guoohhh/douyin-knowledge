"""Answer generation: grounded synthesis with mandatory provenance.

Two modes, one contract.

*Deterministic mode* (no chat model, or ``ai_provider=mock``) composes the answer
from claims and evidence directly. It is genuinely useful rather than a stub:
for the questions this product is built around — 人均多少、几点关门、招牌是什么
— the structured claim set already contains the answer, and rendering it is more
trustworthy than paraphrasing it.

*Model mode* passes retrieved evidence to a chat model with an explicit citation
protocol, then **validates the output**: any ``[n]`` marker the model emits that
does not correspond to a real citation is stripped. A model that cites [7] when
six sources were supplied is the exact failure RETRIEVAL.md 14 forbids, and
prompting alone does not prevent it.

Both modes obey the scope contract: ``personal_required`` and ``personal_first``
answers may only assert what local evidence supports, and an empty retrieval
produces an honest "没找到" with diagnosable reasons instead of a plausible
paragraph (RETRIEVAL.md 15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from douyin_knowledge.ai.providers import ChatMessage
from douyin_knowledge.conversation.citation_builder import CitationSet
from douyin_knowledge.conversation.scope import (
    SCOPE_GENERAL,
    SCOPE_HYBRID,
    ScopeDecision,
)
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from douyin_knowledge.ai.providers import ChatModel
    from douyin_knowledge.retrieval.retriever import RetrievalResult

logger = get_logger(__name__)

MAX_EVIDENCE_IN_PROMPT = 8
MAX_CLAIMS_IN_PROMPT = 20

_SYSTEM_PROMPT = """你是一个个人知识助手。用户的收藏视频已经被处理成结构化证据。

硬性规则：
1. 只能使用下面提供的证据回答。证据里没有的信息，不要补充、不要推测。
2. 每条实质性事实后面必须加引用标记，格式为 [n]，n 是证据编号。
3. 不确定就说不确定。证据不足就直说证据不足。
4. 如果不同来源说法冲突，两种说法都要写出来，并说明它们来自不同来源。
5. 不要把作者的主观评价写成客观事实。原文是"我觉得好吃"就不要写成"很好吃"。
6. 用中文回答，简洁直接，不要客套话。"""

_GENERAL_SYSTEM_PROMPT = """你是一个知识助手。这个问题不是在问用户的收藏内容，
所以你可以用通用知识回答。

硬性规则：
1. 不要暗示答案来自用户的收藏。
2. 不要编造引用标记。
3. 用中文回答，简洁直接。"""

NO_EVIDENCE_TEMPLATE = "我没有在你已经处理的收藏里找到足够证据来回答这个问题。"

#: Display labels for structured constraint fields. Keys are `QueryPlan` field names.
_CONSTRAINT_LABELS = {
    "price_per_person": "人均",
    "cuisine": "菜系",
    "district": "位置",
}

#: Why each structured rejection happened, in the user's terms.
#:
#: These exist because "没找到" is several different situations and they need different
#: user actions: a price that missed the threshold means relax the filter, a
#: ``metadata_only`` source means process it, a superseded run means the answer changed
#: since the video was reprocessed (RETRIEVAL.md 15).
_REJECTION_LABELS = {
    "numeric_constraint_failed": "有匹配的店，但数值不满足你给的条件",
    "text_constraint_failed": "有匹配的店，但类别对不上",
    "missing_required_claim": "找到了相关的店，但收藏里没有对应的结构化信息",
    "no_eligible_claims_metadata_only": (
        "相关收藏只保留了元数据，内容从未被真正理解，所以不能当作已知知识"
    ),
    "no_eligible_claims_excluded": "相关收藏已被排除在知识检索之外",
    "no_eligible_claims_superseded": "相关数值只存在于已被取代的旧处理结果里",
    "no_eligible_claims_downgraded": "相关断言没有通过溯源校验，不能用来回答",
    "entity_type_mismatch": "有条件都对得上的内容，但它不是你问的那类东西",
    "entity_subtype_mismatch": "有条件都对得上的地方，但子类型和你问的不一致",
    # 去过 is deliberately not mentioned: it is not a state V1 can filter on, so a label
    # offering it would advertise a capability the executor does not have.
    "user_state_mismatch": "有符合条件的店，但你还没标记成想去",
}


def _format_value(value: Any) -> str:
    """Render a claim value for display, without inventing precision.

    ``value_number`` is a float column, so a price of 80 arrives as ``80.0``. Printing
    that implies a precision the creator never gave -- they said 人均八十.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass
class GeneratedAnswer:
    """An answer plus everything needed to audit it."""

    content: str
    scope: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    has_evidence: bool = True
    generator: str = "deterministic"
    model_name: str | None = None
    conflicts: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "generator": self.generator,
            "model_name": self.model_name,
            "has_evidence": self.has_evidence,
            "citation_count": len(self.citations),
            "conflicts": self.conflicts,
            "suggestions": self.suggestions,
            "diagnostics": self.diagnostics,
        }


class AnswerGenerator:
    """Turn retrieval output into a cited answer."""

    def __init__(self, *, chat_model: ChatModel | None = None, model_name: str | None = None) -> None:
        self.chat_model = chat_model
        self.model_name = model_name

    # ------------------------------------------------------------ entrypoint

    def generate(
        self,
        query: str,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> GeneratedAnswer:
        if decision.scope == SCOPE_GENERAL:
            return self._general_answer(query, decision)

        if result.is_empty():
            if decision.allows_general_knowledge:
                answer = self._general_answer(query, decision)
                answer.content = (
                    f"{NO_EVIDENCE_TEMPLATE}\n\n以下是不依赖你的收藏的通用回答：\n\n"
                    f"{answer.content}"
                )
                answer.has_evidence = False
                answer.suggestions = self._no_result_suggestions(result)
                return answer
            return self._no_evidence_answer(result, decision)

        if result.structured is not None and result.structured.matches:
            # A structured result is rendered deterministically even when a chat model is
            # available. The answer's job here is to state which entities satisfied which
            # constraint and on whose authority; paraphrasing that through a model can only
            # lose the precision the structured executor just established.
            return self._structured_answer(result, citations, decision)

        if self.chat_model is not None:
            return self._model_answer(query, result, citations, decision)
        return self._deterministic_answer(query, result, citations, decision)

    # ------------------------------------------------------- structured answers

    def _structured_answer(
        self,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> GeneratedAnswer:
        """Render qualifying entities with per-constraint provenance.

        Every qualifying entity gets one line per satisfied constraint, each carrying the
        citation of the claim that satisfied it. That is what makes the four questions the
        brief requires answerable from the answer text alone: why this entity, why this
        district, why this cuisine, which price claim cleared the threshold.

        Conflicts are rendered as their own section rather than folded into the value line.
        A restaurant with an eligible 80 and an eligible 120 qualifies for ``< 100``, and
        writing "人均 80" alone would imply a settled price the archive does not support.
        """
        structured = result.structured
        assert structured is not None  # guarded by the caller

        lines: list[str] = [
            f"在你的收藏里找到 {len(structured.matches)} 个符合条件的结果。",
            "",
        ]
        conflicts: list[str] = []

        for index, match in enumerate(structured.matches, start=1):
            lines.append(f"{index}. {match.entity.canonical_name}")
            for field_name, support in match.supports.items():
                label = _CONSTRAINT_LABELS.get(field_name, field_name)
                # Claims are grouped by (value, attribution) rather than rendered one per
                # claim. The same creator saying 日料 in three sentences produces three
                # eligible claims, and printing three identical lines reads as three
                # independent confirmations when it is one. Distinct values are *not*
                # merged -- that is a conflict and it is rendered below.
                grouped: dict[tuple[str, str], list[str]] = {}
                for claim in support.satisfied_by:
                    citation = citations.by_claim.get(claim.id)
                    value = (
                        claim.value_number
                        if claim.value_number is not None
                        else claim.value_text
                    )
                    key = (_format_value(value), claim.attribution or "未知作者")
                    markers = grouped.setdefault(key, [])
                    if citation is not None and citation.marker not in markers:
                        markers.append(citation.marker)
                for (value_text, attribution), markers in grouped.items():
                    lines.append(
                        f"   - {label}：{value_text}（来自 {attribution}）{''.join(markers)}"
                    )

            for field_name in match.conflicts:
                label = _CONSTRAINT_LABELS.get(field_name, field_name)
                rendered: list[str] = []
                seen: set[tuple[str, str]] = set()
                for claim in match.supports[field_name].all_eligible:
                    citation = citations.by_claim.get(claim.id)
                    marker = citation.marker if citation else ""
                    value = (
                        claim.value_number
                        if claim.value_number is not None
                        else claim.value_text
                    )
                    attribution = claim.attribution or "未知作者"
                    key = (_format_value(value), attribution)
                    if key in seen:
                        continue
                    seen.add(key)
                    rendered.append(f"{attribution}说 {_format_value(value)}{marker}")
                conflicts.append(
                    f"{match.entity.canonical_name} 的{label}在不同来源之间不一致："
                    f"{'；'.join(rendered)}"
                )
        lines.append("")

        if conflicts:
            lines.append("注意：以下信息在不同来源之间存在分歧，需要你自己判断：")
            lines += [f"- {c}" for c in conflicts]
            lines.append("")

        excerpt_lines = self._render_excerpts(result, citations)
        if excerpt_lines:
            lines.append("相关原文：")
            lines += excerpt_lines

        return GeneratedAnswer(
            content="\n".join(lines).rstrip(),
            scope=decision.scope,
            citations=citations.as_list(),
            generator="structured",
            conflicts=conflicts,
            diagnostics=dict(result.diagnostics),
        )

    # --------------------------------------------------------- no-result path

    def _no_evidence_answer(
        self, result: RetrievalResult, decision: ScopeDecision
    ) -> GeneratedAnswer:
        suggestions = self._no_result_suggestions(result)
        lines = [NO_EVIDENCE_TEMPLATE, ""]
        lines.append("可能的原因：")
        for reason in self._no_result_reasons(result):
            lines.append(f"- {reason}")
        if suggestions:
            lines += ["", "你可以试试："]
            lines += [f"- {s}" for s in suggestions]
        return GeneratedAnswer(
            content="\n".join(lines),
            scope=decision.scope,
            has_evidence=False,
            suggestions=suggestions,
            diagnostics=dict(result.diagnostics),
        )

    @staticmethod
    def _no_result_reasons(result: RetrievalResult) -> list[str]:
        """Distinguish the failure modes in RETRIEVAL.md 15.

        "No match" and "matched but not processed deeply enough" require
        completely different user actions, so collapsing them into one message
        leaves the user unable to fix anything.

        Structured rejections are read first and, when present, are the whole answer. They
        are strictly more informative than the retrieval-level diagnostics: "有匹配的店，
        但人均 150 超过了 100" tells the user what to change, while "收藏里没有匹配这个说法
        的内容" is false in that situation and would send them looking for a video they
        already have.
        """
        diagnostics = result.diagnostics or {}
        reasons: list[str] = []

        structured = result.structured
        if structured is not None:
            seen: list[str] = []
            for rejection in structured.rejections:
                label = _REJECTION_LABELS.get(rejection.reason)
                if label is None or label in seen:
                    continue
                seen.append(label)
                detail = (
                    f"{label}（{rejection.entity_name}：{rejection.detail}）"
                    if rejection.detail
                    else label
                )
                reasons.append(detail)
            if not structured.rejections and structured.diagnostics.get("reason") in (
                "no_structured_candidates",
                "plan_not_structured",
            ):
                reasons.append("收藏里没有满足这些条件的对象")
            if reasons:
                return reasons

        if diagnostics.get("reason") == "no_current_chunks":
            reasons.append(
                "相关收藏还没有被处理到可检索的层级（目前只有元数据），"
                "或者处理结果尚未生效"
            )
        if not diagnostics.get("fts_available", False):
            reasons.append("全文索引尚未建立，只能依赖语义检索")
        if diagnostics.get("keyword_hits", 0) == 0 and diagnostics.get("vector_hits", 0) == 0:
            reasons.append("收藏里没有匹配这个说法的内容")
        if diagnostics.get("vector_below_floor", 0) and diagnostics.get("vector_hits", 0) == 0:
            # Worth distinguishing: there *were* neighbours, they were just too
            # weak to trust. That is a different user action than "nothing here".
            reasons.append("有语义上略微接近的内容，但相似度太低，不足以作为依据")
        if not reasons:
            reasons.append("查询条件可能过窄")
        return reasons

    @staticmethod
    def _no_result_suggestions(result: RetrievalResult) -> list[str]:
        return ["放宽筛选条件", "对候选视频做深度处理（level 2+）", "搜索被跳过的收藏"]

    # ------------------------------------------------------- general knowledge

    def _general_answer(self, query: str, decision: ScopeDecision) -> GeneratedAnswer:
        if self.chat_model is None:
            return GeneratedAnswer(
                content=(
                    "这个问题不是在问你的收藏内容。当前没有配置对话模型，"
                    "所以我无法给出通用知识回答。配置 API key 后可以使用这个能力。"
                ),
                scope=decision.scope,
                has_evidence=False,
                generator="deterministic",
            )
        response = self.chat_model.generate(
            [
                ChatMessage(role="system", content=_GENERAL_SYSTEM_PROMPT),
                ChatMessage(role="user", content=query),
            ],
            temperature=0.3,
        )
        # No citations by construction: attaching collection provenance to
        # general knowledge is the fake-provenance failure (RETRIEVAL.md 14).
        return GeneratedAnswer(
            content=_strip_citation_markers(response.content),
            scope=decision.scope,
            has_evidence=False,
            generator="model",
            model_name=response.model,
        )

    # -------------------------------------------------------- deterministic

    def _deterministic_answer(
        self,
        query: str,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> GeneratedAnswer:
        lines: list[str] = []
        conflicts: list[str] = []

        source_count = len(result.source_ids)
        lines.append(f"在你的收藏里找到 {source_count} 个相关来源。")
        lines.append("")

        claim_lines, conflicts = self._render_claims(result, citations)
        if claim_lines:
            lines.append("已知信息：")
            lines += claim_lines
            lines.append("")

        excerpt_lines = self._render_excerpts(result, citations)
        if excerpt_lines:
            lines.append("相关原文：")
            lines += excerpt_lines
            lines.append("")

        if not claim_lines and not excerpt_lines:
            return self._no_evidence_answer(result, decision)

        if conflicts:
            lines.append("注意：以下信息在不同来源之间存在分歧，需要你自己判断：")
            lines += [f"- {c}" for c in conflicts]
            lines.append("")

        return GeneratedAnswer(
            content="\n".join(lines).rstrip(),
            scope=decision.scope,
            citations=citations.as_list(),
            generator="deterministic",
            conflicts=conflicts,
            diagnostics=dict(result.diagnostics),
        )

    def _render_claims(
        self, result: RetrievalResult, citations: CitationSet
    ) -> tuple[list[str], list[str]]:
        """Render claims grouped by predicate so disagreement becomes visible."""
        from douyin_knowledge.wiki.composer import render_claim_value

        by_predicate: dict[str, list[Any]] = {}
        for claim in result.claims:
            by_predicate.setdefault(claim.predicate, []).append(claim)

        lines: list[str] = []
        conflicts: list[str] = []
        for predicate, claims in by_predicate.items():
            values: list[tuple[str, str]] = []
            for claim in claims:
                value = render_claim_value(claim)
                citation = citations.by_claim.get(claim.id)
                marker = citation.marker if citation else ""
                if value:
                    values.append((value, marker))

            if not values:
                continue

            distinct = {v for v, _ in values}
            rendered = "；".join(f"{v}{m}" for v, m in values)
            subject = self._subject_label(claims[0])
            lines.append(f"- {subject} {predicate}：{rendered}")
            if len(distinct) > 1:
                conflicts.append(f"{subject} {predicate} 有 {len(distinct)} 种说法：{rendered}")
        return lines, conflicts

    @staticmethod
    def _subject_label(claim: Any) -> str:
        return (claim.subject_text or "该主题").strip()

    def _render_excerpts(
        self, result: RetrievalResult, citations: CitationSet, limit: int = 4
    ) -> list[str]:
        lines: list[str] = []
        for chunk in result.chunks[:limit]:
            citation = citations.by_chunk.get(chunk.chunk_id)
            if citation is None:
                continue
            snippet = (citation.snippet or chunk.text).strip()
            if not snippet:
                continue
            lines.append(f"- {snippet}{citation.marker}")
        return lines

    # --------------------------------------------------------------- model

    def _model_answer(
        self,
        query: str,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> GeneratedAnswer:
        prompt = self._build_prompt(query, result, citations, decision)
        response = self.chat_model.generate(  # type: ignore[union-attr]
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt),
            ],
            temperature=0.2,
        )

        valid_ordinals = {c.ordinal for c in citations.citations}
        content, removed = _validate_citation_markers(response.content, valid_ordinals)
        if removed:
            # Logged rather than silently accepted: repeated hallucinated
            # markers are a signal the prompt or model needs changing.
            logger.warning(
                "answer_citation_markers_stripped",
                extra={"removed": sorted(removed), "valid": sorted(valid_ordinals)},
            )

        if decision.scope == SCOPE_HYBRID:
            content = f"### 来自你的收藏\n\n{content}"

        return GeneratedAnswer(
            content=content,
            scope=decision.scope,
            citations=citations.as_list(),
            generator="model",
            model_name=response.model,
            diagnostics={**dict(result.diagnostics), "stripped_markers": sorted(removed)},
        )

    def _build_prompt(
        self,
        query: str,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> str:
        from douyin_knowledge.wiki.composer import render_claim_value

        parts: list[str] = ["证据："]
        for chunk in result.chunks[:MAX_EVIDENCE_IN_PROMPT]:
            citation = citations.by_chunk.get(chunk.chunk_id)
            if citation is None:
                continue
            head = f"[{citation.ordinal}] {citation.label or ''}".strip()
            parts.append(f"{head}\n{(citation.snippet or chunk.text).strip()}")

        claim_lines: list[str] = []
        for claim in result.claims[:MAX_CLAIMS_IN_PROMPT]:
            citation = citations.by_claim.get(claim.id)
            marker = f"[{citation.ordinal}]" if citation else ""
            value = render_claim_value(claim)
            subject = self._subject_label(claim)
            provenance = "作者主观评价" if claim.provenance_type == "creator_opinion" else "作者陈述"
            claim_lines.append(f"{marker} {subject} {claim.predicate} = {value}（{provenance}）")
        if claim_lines:
            parts.append("结构化陈述：")
            parts.extend(claim_lines)

        parts.append(f"\n用户问题：{query}")
        if decision.scope == SCOPE_HYBRID:
            parts.append(
                "\n注意：这是混合模式。先只根据上面的证据回答，"
                "通用知识部分会由系统单独追加，不要在这里混入。"
            )
        return "\n\n".join(parts)


def _strip_citation_markers(text: str) -> str:
    """Remove every ``[n]`` from text that must not carry collection citations."""
    import re

    return re.sub(r"\[\d+\]", "", text).strip()


def _validate_citation_markers(text: str, valid: set[int]) -> tuple[str, set[int]]:
    """Drop markers that point at citations we did not supply."""
    import re

    removed: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        ordinal = int(match.group(1))
        if ordinal in valid:
            return match.group(0)
        removed.add(ordinal)
        return ""

    return re.sub(r"\[(\d+)\]", replace, text).strip(), removed


__all__ = ["AnswerGenerator", "GeneratedAnswer", "NO_EVIDENCE_TEMPLATE"]
