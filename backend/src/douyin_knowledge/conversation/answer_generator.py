"""Answer generation: grounded synthesis with mandatory provenance.

Two modes, one contract.

*Deterministic mode* (no chat model, or ``ai_provider=mock``) composes the answer
from claims and evidence directly. It is genuinely useful rather than a stub:
for the questions this product is built around — 人均多少、几点关门、招牌是什么
— the structured claim set already contains the answer, and rendering it is more
trustworthy than paraphrasing it.

*Model mode* passes retrieved evidence to a chat model with an explicit citation
protocol, then **validates the output against the grounding contract** in
``conversation.grounding``. A model that cites [7] when six sources were supplied
is the exact failure RETRIEVAL.md 14 forbids, and prompting alone does not
prevent it. Neither does stripping the bad marker: the sentence it supported is
still an unsupported assertion afterwards, only now nothing marks it as one. So a
violating answer is **refused whole** and the deterministic composition below is
returned in its place. Unsupported model prose is never a normal grounded answer.

Both modes obey the scope contract: ``personal_required`` and ``personal_first``
answers may only assert what local evidence supports, and an empty retrieval
produces an honest "没找到" with diagnosable reasons instead of a plausible
paragraph (RETRIEVAL.md 15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from douyin_knowledge.ai.providers import ChatMessage
from douyin_knowledge.conversation.citation_builder import CitationSet, format_timestamp
from douyin_knowledge.conversation.grounding import (
    unknown_ordinals,
    validate_grounding,
    violations_as_list,
)
from douyin_knowledge.conversation.scope import (
    SCOPE_GENERAL,
    SCOPE_HYBRID,
    ScopeDecision,
)
from douyin_knowledge.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from douyin_knowledge.ai.providers import ChatModel
    from douyin_knowledge.retrieval.retriever import RetrievalResult
    from douyin_knowledge.retrieval.structured import StructuredMatch, StructuredResult

logger = get_logger(__name__)

MAX_EVIDENCE_IN_PROMPT = 8
MAX_CLAIMS_IN_PROMPT = 20

#: How each evidence kind is described to the answer model. Reliability differs between
#: them -- a caption is what the creator typed, ASR is what they said -- and the model
#: cannot weigh that if every snippet is presented identically.
_EVIDENCE_KIND_LABELS = {
    "asr": "语音转写",
    "subtitle": "字幕",
    "title": "视频标题",
    "caption": "视频文案",
    "transcript": "转写文本",
    "paragraph": "正文段落",
    "image": "图片内容",
    "video": "视频内容",
}

_SYSTEM_PROMPT = """你是一个个人知识助手。用户的收藏视频已经被处理成结构化证据。

硬性规则：
1. 只能使用下面提供的证据回答。证据里没有的信息，不要补充、不要推测。
2. 每条实质性事实后面必须加引用标记，格式为 [n]，n 必须是下面出现过的证据编号。
   只允许 [1] 或 [1,2] 这种纯数字形式。不要写 [第五步]、[来源]、[证据] 这类
   方括号标签——它们不是引用，会导致整个回答被判为不合格并丢弃。
3. 不确定就说不确定。证据不足就直说证据不足。宁可少说，不要在没有编号支持的
   情况下陈述事实。
4. 如果不同来源说法冲突，两种说法都要写出来，并说明它们来自不同来源。
5. 不要把作者的主观评价写成客观事实。原文是"我觉得好吃"就不要写成"很好吃"。
6. 证据按「来源」分组。同一个来源块里的条目来自同一个视频。不要把同一来源的
   两条证据描述成两个不同的视频，也不要说其中一条与这个视频无关。
7. 用中文回答，简洁直接，不要客套话。"""

_GENERAL_SYSTEM_PROMPT = """你是一个知识助手。这个问题不是在问用户的收藏内容，
所以你可以用通用知识回答。

硬性规则：
1. 不要暗示答案来自用户的收藏。
2. 不要编造引用标记。
3. 用中文回答，简洁直接。"""

NO_EVIDENCE_TEMPLATE = "我没有在你已经处理的收藏里找到足够证据来回答这个问题。"

#: Said when the collection was *not searched* because the question states a condition V1
#: cannot express. Distinct from `NO_EVIDENCE_TEMPLATE`, which reports the outcome of a
#: search that did run: "找到足够证据" is a finding, and a refused query has no finding.
REFUSED_TEMPLATE = "这个问题里有我无法在你的收藏上执行的筛选条件，所以我没有检索你的收藏"

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


def _partition_by_provable_qualification(
    structured: StructuredResult, citations: CitationSet
) -> tuple[list[StructuredMatch], list[StructuredMatch]]:
    """Split matches into those this answer can prove qualified, and those it cannot.

    Qualifying in the executor and being presentable as qualified are different
    statements. ``1. 小林日料`` under the heading 符合条件 asserts that this entity meets the
    user's hard constraints, and when the claim that cleared 人均 < 100 has no citation in
    the current set, the answer has made that assertion on nothing -- the support lines
    below it are silently missing the very constraint the user asked about.

    So a match is renderable only when *every* claim-derived required constraint has at
    least one citable satisfying claim. Every one, not any: a cited 菜系 line does not
    prove the price, and accepting "some citation somewhere for this entity" would let an
    unrelated excerpt from the same video stand in for the constraint that actually
    decided qualification.

    What is deliberately *not* required:

    * Optional constraints (``required=False``). An optional constraint that cannot be
      cited simply goes unrendered; demanding provenance for it would quietly promote it
      to mandatory and drop results that legitimately qualified without it.
    * Entity type and subtype narrowing. Those are our own annotations, not a creator's
      assertion, so there is no creator provenance to demand.
    * User state (DEC-018). 想去 is the user's own declaration; a user-state-only match
      has no claim-derived required field at all, so this function returns it unchanged.

    `StructuredResult` is not mutated. Retrieval diagnostics must keep reporting what the
    executor actually found, so the withheld matches stay in `structured.matches` and only
    the *rendering* is narrowed.
    """
    required_fields = structured.plan.required_claim_fields
    renderable: list[StructuredMatch] = []
    withheld: list[StructuredMatch] = []
    for match in structured.matches:
        provable = True
        for field_name in required_fields:
            support = match.supports.get(field_name)
            # A missing support entry for a *required* field should be unreachable: the
            # executor rejects a candidate that cannot satisfy one. Treated as unprovable
            # rather than skipped, because the fallback for an unexpected shape has to be
            # the safe direction, and `if field_name in match.supports` would have silently
            # made an absent constraint count as satisfied.
            if support is None or not citations.renderable_claims(support.satisfied_by):
                provable = False
                break
        (renderable if provable else withheld).append(match)
    return renderable, withheld


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
                # Which preamble depends on whether the collection was *searched*.
                # `NO_EVIDENCE_TEMPLATE` says 我没有在你已经处理的收藏里找到足够证据 -- a
                # claim about what a search found. On a refused condition no search ran, so
                # that sentence reports a check that never happened. The general half is
                # still offered either way; only the account of the collection half changes.
                refusal = self._refusal_message(result)
                preamble = (
                    f"{REFUSED_TEMPLATE}（{refusal}）"
                    if refusal is not None
                    else NO_EVIDENCE_TEMPLATE
                )
                answer.content = (
                    f"{preamble}\n\n以下是不依赖你的收藏的通用回答：\n\n{answer.content}"
                )
                answer.has_evidence = False
                answer.suggestions = self._no_result_suggestions(result)
                answer.diagnostics = dict(result.diagnostics)
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

        renderable, withheld = _partition_by_provable_qualification(structured, citations)

        lines: list[str] = [
            f"在你的收藏里找到 {len(renderable)} 个符合条件的结果。",
            "",
        ]
        conflicts: list[str] = []

        for index, match in enumerate(renderable, start=1):
            lines.append(f"{index}. {match.entity.canonical_name}")
            for field_name, support in match.supports.items():
                label = _CONSTRAINT_LABELS.get(field_name, field_name)
                # Claims are grouped by (value, attribution) rather than rendered one per
                # claim. The same creator saying 日料 in three sentences produces three
                # eligible claims, and printing three identical lines reads as three
                # independent confirmations when it is one. Distinct values are *not*
                # merged -- that is a conflict and it is rendered below.
                grouped: dict[tuple[str, str], list[str]] = {}
                # Citable claims only: a constraint line states a value on a creator's
                # authority, so an uncitable claim has no authority to state it with. With
                # the unfiltered list, a group whose every claim missed the citation budget
                # produced a line with no marker at all.
                for claim in citations.renderable_claims(support.satisfied_by):
                    citation = citations.by_claim[claim.id]
                    value = (
                        claim.value_number
                        if claim.value_number is not None
                        else claim.value_text
                    )
                    key = (_format_value(value), claim.attribution or "未知作者")
                    markers = grouped.setdefault(key, [])
                    if citation.marker not in markers:
                        markers.append(citation.marker)
                for (value_text, attribution), markers in grouped.items():
                    lines.append(
                        f"   - {label}：{value_text}（来自 {attribution}）{''.join(markers)}"
                    )

            for field_name in match.conflicts:
                label = _CONSTRAINT_LABELS.get(field_name, field_name)
                rendered: list[str] = []
                seen: set[tuple[str, str]] = set()
                for claim in citations.renderable_claims(
                    match.supports[field_name].all_eligible
                ):
                    citation = citations.by_claim[claim.id]
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
                    rendered.append(
                        f"{attribution}说 {_format_value(value)}{citation.marker}"
                    )
                # A disagreement needs at least two citable sides to be reportable. Below
                # that there is nothing to attribute the disagreement *to*, and the honest
                # move is silence rather than a conflict notice quoting one value.
                #
                # Known accepted gap: if two values conflict and only one is citable, the
                # support line above states the citable one with no conflict notice, which
                # reads more settled than the archive is. Grounding is preserved -- every
                # rendered value is attributable -- but completeness is not. Stating an
                # uncitable value to flag the conflict would trade a real guarantee for a
                # softer one, so the gap is documented rather than closed that way.
                if len({value for value, _ in seen}) < 2:
                    continue
                conflicts.append(
                    f"{match.entity.canonical_name} 的{label}在不同来源之间不一致："
                    f"{'；'.join(rendered)}"
                )
        lines.append("")

        if withheld:
            # A system statement about this answer's own limits, not a creator fact, so it
            # needs no citation. Saying nothing would be the dishonest option in the other
            # direction: the executor did find these, and silently dropping them would
            # misreport the search as having found less than it did.
            lines.append(
                f"另有 {len(withheld)} 个结果在本次回答里拿不到可核查的出处，"
                "因此没有列为符合条件的结果。"
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
            # Derived, not defaulted. A user-state-only match qualifies out of
            # `EntityUserState` and cites nothing (DEC-018: the user's own intent needs no
            # creator claim behind it), so the default `True` announced evidence for an
            # answer whose citation list was empty -- the same overclaim the structured path
            # exists to prevent, one field over.
            has_evidence=bool(citations),
            diagnostics=dict(result.diagnostics),
        )

    # --------------------------------------------------------- no-result path

    def _no_evidence_answer(
        self, result: RetrievalResult, decision: ScopeDecision
    ) -> GeneratedAnswer:
        suggestions = self._no_result_suggestions(result)
        refusal = self._refusal_message(result)
        if refusal is not None:
            # Same distinction the hybrid branch draws: no search ran, so there is no
            # finding to report and no list of reasons a search might have come back
            # empty. The refused condition is the whole account of this turn.
            lines = [f"{REFUSED_TEMPLATE}（{refusal}）"]
        else:
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
    def _refusal_message(result: RetrievalResult) -> str | None:
        """The refusal message if this turn refused a condition, else ``None``.

        One reader for both consumers -- the no-result reason list and the hybrid preamble --
        because they must agree on whether a search happened. They disagreed before: the
        personal path surfaced the refusal while the hybrid path reported an empty search.
        """
        refused = (result.diagnostics or {}).get("refused")
        if isinstance(refused, dict):
            message = refused.get("message")
            if isinstance(message, str) and message:
                return message
        return None

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

        # A refusal outranks everything below it. The retrieval-level reasons all describe a
        # search that ran and found nothing; a refused query never ran one, and saying
        # 收藏里没有匹配这个说法的内容 would claim an absence that was never checked.
        refusal = AnswerGenerator._refusal_message(result)
        if refusal is not None:
            return [refusal]

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
        """Render claims grouped by predicate so disagreement becomes visible.

        Only claims the current `CitationSet` can cite are rendered. This is the line the
        real Stage 3C failure crossed: `微调阶段 requires_mastering：Lora QLora Prefix Tuning
        等一系列微调技术` reached the user with no marker, and from that text there was no
        route back to an evidence unit, a source or a timestamp. A fact that cannot be
        attributed is dropped, not stated bare -- completeness may degrade, grounding may not.
        """
        from douyin_knowledge.wiki.composer import render_claim_value

        by_predicate: dict[str, list[Any]] = {}
        for claim in citations.renderable_claims(result.claims):
            by_predicate.setdefault(claim.predicate, []).append(claim)

        lines: list[str] = []
        conflicts: list[str] = []
        for predicate, claims in by_predicate.items():
            values: list[tuple[str, str]] = []
            for claim in claims:
                value = render_claim_value(claim)
                citation = citations.by_claim[claim.id]
                if value:
                    values.append((value, citation.marker))

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
        """Quote only chunks this citation set can attribute.

        This path was already correct; it now asks `CitationSet` the same question the other
        renderers ask, so the rule has one definition instead of four.

        `limit` counts quoted lines, not chunks. One chunk can project several evidence
        citations, and the thing worth bounding is how much text the user reads -- bounding
        chunks instead would have let one broad chunk's window set the answer's length.
        Quoting the whole window rather than one unit of it is what lets 八元。 appear next
        to the 手抓饼。 it answers: each line carries its own marker and its own timestamp,
        so no line borrows another's provenance.
        """
        lines: list[str] = []
        for chunk in citations.renderable_chunks(result.chunks):
            for citation in citations.citations_for_chunk(chunk.chunk_id):
                if len(lines) >= limit:
                    return lines
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
        violations = validate_grounding(response.content, valid_ordinals)
        removed = unknown_ordinals(response.content, valid_ordinals)

        if violations:
            # Fail closed. The model's prose is discarded whole rather than repaired,
            # because every available repair leaves an unsupported assertion standing:
            # deleting `[99]` from 还有分店[99] yields 还有分店, which asserts the same
            # unsupported thing with the evidence of its unsupportedness removed. The
            # deterministic answer below is composed from claims and evidence by this
            # module, so it cannot be ungrounded -- no model wrote it.
            logger.warning(
                "answer_grounding_contract_violated",
                extra={
                    "violations": violations_as_list(violations),
                    "valid": sorted(valid_ordinals),
                    "model": response.model,
                },
            )
            fallback = self._deterministic_answer(query, result, citations, decision)
            fallback.generator = "deterministic_fallback"
            fallback.model_name = response.model
            fallback.diagnostics = {
                **fallback.diagnostics,
                "stripped_markers": sorted(removed),
                "grounding_violations": violations_as_list(violations),
                "rejected_model_answer": True,
            }
            if decision.scope == SCOPE_HYBRID:
                fallback.content = f"### 来自你的收藏\n\n{fallback.content}"
            return fallback

        content = response.content.strip()
        if decision.scope == SCOPE_HYBRID:
            content = f"### 来自你的收藏\n\n{content}"

        return GeneratedAnswer(
            content=content,
            scope=decision.scope,
            citations=citations.as_list(),
            generator="model",
            model_name=response.model,
            diagnostics={
                **dict(result.diagnostics),
                "stripped_markers": sorted(removed),
                "grounding_violations": [],
            },
        )

    def _build_prompt(
        self,
        query: str,
        result: RetrievalResult,
        citations: CitationSet,
        decision: ScopeDecision,
    ) -> str:
        """Present evidence grouped by source, because a flat list hides co-origin.

        The 手抓饼 answer said the 芹菜 evidence was "unrelated to the 手抓饼 video" when
        ``Claim.source_id``, ``EvidenceUnit.source_id`` and the retrieved chunk were all
        that one source. Nothing was wrong with the provenance; the *prompt* was a flat
        interleaved list of ``[n] label`` lines, and a model reading it has no way to tell
        which snippets came from the same video -- two entries from one source look exactly
        like one entry each from two. So it guessed, and guessed that they were unrelated.

        Grouping is a presentation change and nothing more. Every marker still comes from
        `CitationBuilder`; this method reads ordinals and never mints one, so the
        no-fake-provenance guarantee is untouched. What changes is that source identity,
        evidence kind, timestamp and claim provenance are all stated explicitly instead of
        being implicit in an ordering the model cannot see.
        """
        from douyin_knowledge.wiki.composer import render_claim_value

        parts: list[str] = [
            "证据（按来源分组。同一个「来源」块内的所有条目来自同一个视频，"
            "不是不同的视频；不要把它们说成互不相关的来源）："
        ]

        # Source order follows first appearance in retrieval order, so the most relevant
        # source is still presented first and marker numbers still ascend with relevance.
        # `MAX_EVIDENCE_IN_PROMPT` bounds evidence *lines*, not chunks: one chunk projects a
        # window of several evidence citations and the model has to see all of them, because
        # the unit that answers the question is routinely not the unit that matched it. On
        # the real failure, showing one unit per chunk is what sent the model
        # `[1]（语音转写 @ 01:55）补钙啊？` in answer to 手抓饼多少钱.
        grouped: dict[str, list[tuple[Any, Any]]] = {}
        shown = 0
        for chunk in citations.renderable_chunks(result.chunks):
            if shown >= MAX_EVIDENCE_IN_PROMPT:
                break
            for citation in citations.citations_for_chunk(chunk.chunk_id):
                if shown >= MAX_EVIDENCE_IN_PROMPT:
                    break
                grouped.setdefault(chunk.source_id, []).append((chunk, citation))
                shown += 1

        # Filtered to citable claims *before* the display budget is applied. An uncitable
        # claim shown here invites the model to assert a fact it has no ordinal for, which
        # the grounding validator then rejects -- so the whole answer is lost to a claim that
        # could never have been cited anyway. Truncating first would also let an uncitable
        # claim take a prompt slot from a citable one.
        renderable = citations.renderable_claims(result.claims)
        claims_by_source: dict[str, list[Any]] = {}
        for claim in renderable[:MAX_CLAIMS_IN_PROMPT]:
            claims_by_source.setdefault(claim.source_id, []).append(claim)

        ordered_sources = list(grouped)
        for source_id in claims_by_source:
            if source_id not in ordered_sources:
                ordered_sources.append(source_id)

        for index, source_id in enumerate(ordered_sources, start=1):
            entries = grouped.get(source_id, [])
            title = next(
                (c.source_title for _, c in entries if c.source_title),
                None,
            ) or next(
                (
                    citations.by_claim[claim.id].source_title
                    for claim in claims_by_source.get(source_id, [])
                    if claim.id in citations.by_claim
                    and citations.by_claim[claim.id].source_title
                ),
                None,
            )
            block: list[str] = [
                f"来源 {index}：{title or '未命名来源'}（source_id={source_id}）"
            ]
            for chunk, citation in entries:
                kind = _EVIDENCE_KIND_LABELS.get(chunk.chunk_type, chunk.chunk_type)
                timestamp = format_timestamp(citation.start_ms)
                stamp = f" @ {timestamp}" if timestamp else ""
                block.append(
                    f"  [{citation.ordinal}]（{kind}{stamp}）"
                    f"{(citation.snippet or chunk.text).strip()}"
                )
            for claim in claims_by_source.get(source_id, []):
                # Guaranteed present: `renderable_claims` filtered this list above.
                marker = f"[{citations.by_claim[claim.id].ordinal}]"
                provenance = (
                    "作者主观评价"
                    if claim.provenance_type == "creator_opinion"
                    else "作者陈述"
                )
                attribution = claim.attribution or "未知作者"
                block.append(
                    f"  {marker} 结构化陈述：{self._subject_label(claim)} "
                    f"{claim.predicate} = {render_claim_value(claim)}"
                    f"（{provenance}，来自 {attribution}）"
                )
            parts.append("\n".join(block))

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


__all__ = [
    "AnswerGenerator",
    "GeneratedAnswer",
    "NO_EVIDENCE_TEMPLATE",
    "REFUSED_TEMPLATE",
]
