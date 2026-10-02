"""Stage 3C: a personal answer may assert only what the current CitationSet can cite.

The [[grounded-answer]] validator from the previous change did its job -- invalid DeepSeek
prose was rejected and never reached the user. Then the *deterministic fallback it fell back
to* turned out to be ungrounded itself. The real answer contained:

    微调阶段 requires_mastering：Lora QLora Prefix Tuning 等一系列微调技术

with no citation marker anywhere on it. The chain was: statement -> no ordinal -> no
Citation -> no EvidenceUnit, source or timestamp reachable from the answer at all.

The contract mismatch underneath is that `RetrievalResult.claims` is not the set of claims
that may be asserted to a user. Retrieval relevance and user-assertability are different
questions, and every renderer was answering the first one. Each did effectively::

    citation = citations.by_claim.get(claim.id)
    marker = citation.marker if citation else ""
    ...render the fact anyway

so an uncitable claim rendered as a bare assertion. Renderable knowledge is
``RetrievalResult ∩ current CitationSet``, and when the two disagree the fact is *omitted*.
Completeness may degrade; grounding may not.

Two things produced the real failure, and only the first was expected:

1. The renderers above, which assert claims they cannot cite.

2. `CitationBuilder.build` used `break` where it needed `continue` at the claim budget
   check. Reusing an already-minted citation costs no ordinal, so a budget check has no
   business ending that loop -- but one claim that could not reuse aborted it, denying free
   reuse to every later claim. Measured on the real shape: 14 claims, 12 of whose evidence
   was *already cited*, and `by_claim` came back empty.

That second point is why "raise the limit from 12" is the wrong fix. It does make the
symptom go away, which is exactly what makes it dangerous: the bug is an unguarded renderer
and a loop that quits early, and neither is about how many slots exist. These tests
therefore keep the default budget and build a corpus that exceeds it.

Every assertion here is on rendered user-visible output -- answer text, `has_evidence`, the
citation list, or the prompt the model was actually handed.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.answer_generator import AnswerGenerator
from douyin_knowledge.conversation.citation_builder import CitationBuilder, CitationSet
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.retrieval.retriever import HybridRetriever, RetrievalResult
from tests.test_grounded_answer_contract import ScriptedChat
from tests.test_structured_retrieval import Corpus, _index

# The real 微调阶段 claim, verbatim from the Stage 3C failure.
LORA = "Lora QLora Prefix Tuning 等一系列微调技术"

# Every chunk carries this phrase so one query reaches the whole roadmap. A query with
# partial overlap retrieves one or two chunks and reproduces nothing -- the budget has to be
# genuinely exhausted for any of this to be a test.
ROADMAP_QUERY = "需要掌握"

ROADMAP = [
    ("预训练阶段", "海量文本自监督训练"),
    ("指令微调阶段", "SFT 指令数据对齐"),
    ("对齐阶段", "RLHF 人类反馈强化学习"),
    ("微调阶段", LORA),
    ("推理阶段", "vLLM 推理加速部署"),
    ("评测阶段", "MMLU 基准评测"),
    ("提示工程", "Chain of Thought 思维链"),
    ("检索增强", "RAG 向量检索增强生成"),
    ("智能体", "ReAct 工具调用循环"),
    ("多模态", "CLIP 图文对齐"),
    ("蒸馏", "知识蒸馏小模型"),
    ("量化", "INT8 权重量化"),
    ("长上下文", "RoPE 位置编码外推"),
    ("安全对齐", "红队测试与越狱防护"),
]


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


class Roadmap:
    """One long video whose claims outnumber the citation budget.

    This is the real Agent-learning shape: a single source, many timestamped ASR segments,
    one extracted claim per segment. Chunks are cited first and in retrieval order, so a
    source with more chunks than the budget starves its own claims -- which is how a claim
    with perfectly good provenance ends up with no reachable citation.
    """

    def __init__(self, corpus: Corpus, topics: list[tuple[str, str]]) -> None:
        self.corpus = corpus
        self.session = corpus.session
        self.source, self.run = corpus.source("Agent 学习路线 完整版")
        self.entity: Entity = corpus.entity("Agent 学习", entity_type="topic")
        self.claims: list[Claim] = []
        self.by_value: dict[str, Claim] = {}
        for index, (stage, detail) in enumerate(topics, start=1):
            self.claims.append(self._segment(index, stage, detail))
            self.by_value[detail] = self.claims[-1]

    def _segment(self, index: int, stage: str, detail: str) -> Claim:
        text = f"{stage} {ROADMAP_QUERY} {detail}"
        start_ms = index * 20_000
        evidence = EvidenceUnit(
            source_id=self.source.id,
            kind="asr",
            start_ms=start_ms,
            end_ms=start_ms + 3_000,
            raw_text=text,
            normalized_text=text,
            content_hash=f"ev_road_{index}",
        )
        self.session.add(evidence)
        self.session.flush()

        chunk = RetrievalChunk(
            source_id=self.source.id,
            processing_run_id=self.run.id,
            chunk_type="asr",
            ordinal=index,
            text=text,
            start_ms=start_ms,
            end_ms=start_ms + 3_000,
            content_hash=f"ch_road_{index}",
        )
        self.session.add(chunk)
        self.session.flush()
        self.session.add(
            RetrievalChunkEvidence(retrieval_chunk_id=chunk.id, evidence_id=evidence.id)
        )

        claim = self.corpus.claim(
            self.entity,
            self.source,
            self.run,
            predicate="requires_mastering",
            text=detail,
            attribution="讲师",
            evidence_text=text,
        )
        # The claim must rest on the *chunk's* evidence, not only on the private unit
        # `Corpus.claim` mints: `_claims_for_chunks` joins through shared evidence, so a
        # claim linked only to its own unit is invisible to retrieval.
        self.session.add(
            ClaimEvidence(
                claim_id=claim.id, evidence_id=evidence.id, support_role="supports"
            )
        )
        self.session.flush()
        return claim


def _paragraphs(corpus: Corpus, source, run, texts: list[str]) -> None:
    """Several paragraph chunks on one run.

    `Corpus.chunk` hardcodes ``ordinal=0``, so calling it twice for one run violates the
    unique index on (run, chunk_type, ordinal). These fixtures need many chunks on a single
    source -- that is the whole mechanism by which chunks exhaust the budget ahead of claims.
    """
    evidence_ids = [
        row.id
        for row in corpus.session.scalars(
            __import__("sqlalchemy").select(EvidenceUnit).where(
                EvidenceUnit.source_id == source.id
            )
        )
    ]
    for index, text in enumerate(texts, start=1):
        chunk = RetrievalChunk(
            source_id=source.id,
            processing_run_id=run.id,
            chunk_type="paragraph",
            ordinal=index,
            text=text,
            content_hash=f"cp_{run.id}_{index}",
        )
        corpus.session.add(chunk)
        corpus.session.flush()
        for evidence_id in evidence_ids:
            corpus.session.add(
                RetrievalChunkEvidence(
                    retrieval_chunk_id=chunk.id, evidence_id=evidence_id
                )
            )
        corpus.session.flush()


def _retrieve(
    session: Session, query: str, *, limit: int = 20
) -> tuple[RetrievalResult, CitationSet]:
    _index(session)
    result = HybridRetriever(session).retrieve(
        query, limit=limit, use_vector=False, include_claims=True
    )
    return result, CitationBuilder(session).build(result)


def _section(content: str, header: str) -> list[str]:
    """The `- ` lines belonging to one section only.

    Sections are delimited by the next header or blank line, not by end-of-answer: the
    conflict block follows 相关原文 in `_deterministic_answer`, so a naive partition on the
    header swallows it and the test ends up asserting about the wrong lines.
    """
    body = content.partition(header)[2]
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            # The newline directly after the header is not the end of the section.
            if lines:
                break
            continue
        if not line.startswith("- "):
            break
        lines.append(line)
    return lines


def _uncited(result: RetrievalResult, citations: CitationSet) -> list[Claim]:
    return [c for c in result.claims if c.id not in citations.by_claim]


def _cited(result: RetrievalResult, citations: CitationSet) -> list[Claim]:
    return [c for c in result.claims if c.id in citations.by_claim]


def _values(claims: list[Claim]) -> list[str]:
    return [c.value_text for c in claims if c.value_text]


def _leaked(text: str, claims: list[Claim], cited: list[Claim] | None = None) -> list[str]:
    """Uncited values present in `text`, excluding values a cited claim also carries.

    Substring matching cannot attribute a repeated value. Eight creators each saying 日料
    produces eight claims with one value, and when some are cited and some are not, finding
    日料 in the answer proves nothing about which claim put it there. Values that any cited
    claim also holds are therefore excluded; `test_every_constraint_line_carries_a_marker`
    is what covers those, by asserting on the line's provenance instead of its content.
    """
    safe = set(_values(cited)) if cited else set()
    return [v for v in _values(claims) if v in text and v not in safe]


def _markers(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"\[(\d+)\]", text)]


def _assert_every_fact_cited(
    content: str, result: RetrievalResult, citations: CitationSet
) -> None:
    """No uncitable claim value appears anywhere in the rendered answer."""
    leaks = _leaked(content, _uncited(result, citations), _cited(result, citations))
    assert leaks == [], f"uncited facts reached the user: {leaks}"


# ============================================== A. the budget is genuinely exceeded


class TestTheBudgetIsActuallyExhausted:
    """Without this, every test below would pass for the wrong reason.

    If the fixture quietly fit inside the citation budget, every claim would be citable and
    the renderers would look correct while still containing the bug. So the precondition is
    asserted directly, at the default budget, before anything else is measured.
    """

    def test_more_claims_exist_than_the_budget_can_cite(
        self, session, corpus
    ) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        assert len(result.claims) == len(ROADMAP) == 14
        assert len(citations.citations) <= 12, "default budget must still be in force"
        assert len(citations.by_claim) < len(result.claims), (
            "fixture must genuinely exhaust the budget, not merely be large"
        )

    def test_only_a_subset_receives_by_claim_entries(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        uncited = _uncited(result, citations)
        assert uncited, "at least one claim must be uncitable for this suite to mean anything"
        assert len(citations.by_claim) > 0, (
            "and the reusable majority must still get citations -- an empty by_claim here "
            "is the break-instead-of-continue bug, not a budget limit"
        )

    def test_the_real_lora_claim_is_among_the_retrieved(
        self, session, corpus
    ) -> None:
        Roadmap(corpus, ROADMAP)
        result, _ = _retrieve(session, ROADMAP_QUERY)
        assert LORA in _values(list(result.claims))


# ================================================= B. deterministic fallback


class TestDeterministicFallbackRendersOnlyCitedClaims:
    """The path the grounding validator falls back to must itself be grounded.

    This is the actual Stage 3C failure: the model answer was correctly rejected, and the
    replacement was ungrounded.
    """

    def test_fallback_after_rejection_contains_no_uncited_claim(
        self, session, corpus
    ) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括这些阶段[第五步]。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert answer.generator == "deterministic_fallback"
        _assert_every_fact_cited(answer.content, result, citations)

    def test_the_real_lora_statement_does_not_reach_the_user(
        self, session, corpus
    ) -> None:
        """The verbatim regression: this exact string was user-visible and uncited."""
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括这些阶段[第五步]。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        lora_claim = next(c for c in result.claims if c.value_text == LORA)
        if lora_claim.id not in citations.by_claim:
            assert LORA not in answer.content, (
                "the 微调阶段 claim has no citation, so it must not be asserted"
            )

    def test_every_rendered_fact_line_carries_a_marker(
        self, session, corpus
    ) -> None:
        """Structural form of the same rule, independent of which claims got slots."""
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括这些阶段[第五步]。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        for line in answer.content.splitlines():
            if line.startswith("- 该主题") or "requires_mastering" in line:
                assert _markers(line), f"factual line without provenance: {line!r}"

    def test_markers_resolve_to_real_citations(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括这些阶段[第五步]。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        valid = {c.ordinal for c in citations.citations}
        assert set(_markers(answer.content)) <= valid


# ============================================== C. ordinary deterministic answer


class TestOrdinaryDeterministicAnswerHoldsTheSameLine:
    """No chat model configured at all -- the same invariant, a different entry point."""

    def test_no_uncited_claim_is_rendered(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert answer.generator == "deterministic"
        _assert_every_fact_cited(answer.content, result, citations)

    def test_conflict_section_carries_no_uncited_value(
        self, session, corpus
    ) -> None:
        """The leak appeared twice: in 已知信息 *and* in the conflict list."""
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        uncited = _uncited(result, citations)
        for conflict in answer.conflicts:
            assert _leaked(conflict, uncited) == []

    def test_the_answer_still_says_something(self, session, corpus) -> None:
        """Grounding wins over completeness, but the cited majority must still render.

        A fix that silently emptied the answer would pass every leak assertion above while
        making the feature useless.
        """
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert answer.has_evidence is True
        assert _markers(answer.content), "a grounded answer must cite something"
        cited_values = [
            c.value_text
            for c in result.claims
            if c.id in citations.by_claim and c.value_text
        ]
        assert any(v in answer.content for v in cited_values), (
            "cited claims must still be reported"
        )


# ======================================================== D. the model prompt


class TestModelPromptExcludesUncitableClaims:
    """An uncitable claim in the prompt invites the model to assert it with no ordinal.

    This is upstream of the validator: the cheapest way to stop the model producing
    unsupportable prose is to stop showing it unsupportable facts.
    """

    def test_no_uncitable_claim_appears_in_the_prompt(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括预训练[1]。")
        AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        uncited = _uncited(result, citations)
        structured_lines = [
            line for line in chat.prompt.splitlines() if "结构化陈述" in line
        ]
        for line in structured_lines:
            assert _leaked(line, uncited) == [], (
                f"uncitable claim offered to the model: {line!r}"
            )

    def test_every_structured_statement_line_has_a_marker(
        self, session, corpus
    ) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括预训练[1]。")
        AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        for line in chat.prompt.splitlines():
            if "结构化陈述" in line:
                assert _markers(line), f"claim shown with no ordinal: {line!r}"

    def test_cited_claims_are_still_offered(self, session, corpus) -> None:
        """Filtering must not empty the prompt of structured knowledge."""
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("学习路线包括预训练[1]。")
        AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert "结构化陈述" in chat.prompt


# ============================================ E/F. rejection and normal operation


class TestRejectionFallbackIsFullyGrounded:
    """E: an invalid model answer yields a fallback of only cited material."""

    def test_fallback_content_is_entirely_cited(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("这些都是必须掌握的技术，另外还要学会部署。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert answer.generator == "deterministic_fallback"
        assert "另外还要学会部署" not in answer.content
        _assert_every_fact_cited(answer.content, result, citations)
        assert answer.diagnostics.get("rejected_model_answer") is True


class TestCitedFactsStillRenderNormally:
    """F: the ordinary case must be untouched by all of this."""

    def test_a_small_corpus_renders_every_claim(self, session, corpus) -> None:
        """Inside budget, nothing is dropped."""
        Roadmap(corpus, ROADMAP[:3])
        result, citations = _retrieve(session, ROADMAP_QUERY)
        assert _uncited(result, citations) == [], "3 claims must fit the budget"
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        for value in _values(list(result.claims)):
            assert value in answer.content
        assert answer.has_evidence is True

    def test_a_valid_model_answer_is_returned_as_is(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP[:3])
        result, citations = _retrieve(session, ROADMAP_QUERY)
        chat = ScriptedChat("需要掌握海量文本自监督训练[1]。")
        answer = AnswerGenerator(chat_model=chat).generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert answer.generator == "model"
        assert "海量文本自监督训练" in answer.content


# ==================================================== G/H. structured answers


def _crowded_restaurant(corpus: Corpus, name: str = "小林日料") -> Entity:
    """A qualifying entity whose supporting claims outnumber the citation budget.

    Repeated claims are the realistic case, not a contrivance: a creator who says 日料 in
    several sentences produces several eligible claims, and extraction keeps each one.
    """
    source, run = corpus.source(f"{name} 探店")
    entity = corpus.entity(name)
    entity.subtype = "restaurant"
    corpus.session.flush()
    corpus.claim(
        entity, source, run, predicate="located_in", text="徐汇区",
        evidence_text=f"{name} 在徐汇区",
    )
    for index in range(8):
        corpus.claim(
            entity, source, run, predicate="cuisine", text="日料",
            attribution=f"阿明{index}", evidence_text=f"{name} 是日料 第{index}次说",
        )
    corpus.claim(
        entity, source, run, predicate="price_per_person", number=80,
        evidence_text=f"{name} 人均 80",
    )
    _paragraphs(
        corpus,
        source,
        run,
        [f"{name} 日料 徐汇区 人均 80 探店 第{index}段" for index in range(6)],
    )
    return entity


def _structured(session: Session, query: str):
    from douyin_knowledge.retrieval.query_parser import parse_query

    _index(session)
    plan, _ = parse_query(query, scope="personal_required", limit=10)
    result = HybridRetriever(session).retrieve_structured(
        plan, use_vector=False
    )
    return result, CitationBuilder(session).build(result)


class TestStructuredAnswerNeverEmitsAnUncitedFact:
    """G: constraint support lines must not assert a value they cannot cite."""

    def test_no_uncited_support_value_is_rendered(self, session, corpus) -> None:
        _crowded_restaurant(corpus)
        query = "帮我找徐汇区人均100以下的日料"
        result, citations = _structured(session, query)
        assert result.structured is not None and result.structured.matches
        answer = AnswerGenerator().generate(
            query, result, citations, classify_scope(query)
        )
        assert answer.generator == "structured"
        _assert_every_fact_cited(answer.content, result, citations)

    def test_every_constraint_line_carries_a_marker(self, session, corpus) -> None:
        _crowded_restaurant(corpus)
        query = "帮我找徐汇区人均100以下的日料"
        result, citations = _structured(session, query)
        answer = AnswerGenerator().generate(
            query, result, citations, classify_scope(query)
        )
        for line in answer.content.splitlines():
            if line.strip().startswith("- ") and "：" in line and "来自" in line:
                assert _markers(line), f"constraint asserted with no citation: {line!r}"

    def test_the_structured_answer_still_reports_its_match(
        self, session, corpus
    ) -> None:
        _crowded_restaurant(corpus)
        query = "帮我找徐汇区人均100以下的日料"
        result, citations = _structured(session, query)
        answer = AnswerGenerator().generate(
            query, result, citations, classify_scope(query)
        )
        assert "小林日料" in answer.content
        assert answer.has_evidence is True


class TestConflictRenderingIsCitationBacked:
    """H: a conflicting value with no citation must not appear in the conflict list."""

    def test_no_uncited_conflicting_value(self, session, corpus) -> None:
        source, run = corpus.source("小林日料 探店")
        entity = corpus.entity("小林日料")
        entity.subtype = "restaurant"
        session.flush()
        corpus.claim(
            entity, source, run, predicate="located_in", text="徐汇区",
            evidence_text="小林日料 在徐汇区",
        )
        corpus.claim(
            entity, source, run, predicate="cuisine", text="日料",
            evidence_text="小林日料 是日料",
        )
        # Two eligible prices: a real conflict. Plus enough padding claims and chunks that
        # the budget cannot cover both sides of it.
        corpus.claim(
            entity, source, run, predicate="price_per_person", number=80,
            attribution="阿明", evidence_text="小林日料 人均 80",
        )
        corpus.claim(
            entity, source, run, predicate="price_per_person", number=95,
            attribution="小红", evidence_text="小林日料 人均 95",
        )
        for index in range(10):
            corpus.claim(
                entity, source, run, predicate="cuisine", text="日料",
                attribution=f"路人{index}", evidence_text=f"小林日料 日料 第{index}",
            )
        _paragraphs(
            corpus,
            source,
            run,
            [f"小林日料 日料 徐汇区 人均 探店 第{index}段" for index in range(10)],
        )

        query = "帮我找徐汇区人均100以下的日料"
        result, citations = _structured(session, query)
        answer = AnswerGenerator().generate(
            query, result, citations, classify_scope(query)
        )
        uncited = _uncited(result, citations)
        cited = _cited(result, citations)
        for conflict in answer.conflicts:
            assert _leaked(conflict, uncited, cited) == []
            for number in re.findall(r"\d+", conflict):
                if number.isdigit() and len(number) >= 2:
                    assert _markers(conflict), (
                        f"conflicting value stated with no provenance: {conflict!r}"
                    )

    def test_a_fully_citable_conflict_still_renders(self, session, corpus) -> None:
        """The conflict feature must survive the fix.

        `test_case_5_conflicting_prices_stay_visible` in the structured suite covers this
        too; repeating it here pins that the citation filter is what changed and the
        conflict semantics are not.
        """
        source, run = corpus.source("小林日料 探店")
        entity = corpus.entity("小林日料")
        entity.subtype = "restaurant"
        session.flush()
        corpus.claim(
            entity, source, run, predicate="located_in", text="徐汇区",
            evidence_text="小林日料 在徐汇区",
        )
        corpus.claim(
            entity, source, run, predicate="cuisine", text="日料",
            evidence_text="小林日料 是日料",
        )
        corpus.claim(
            entity, source, run, predicate="price_per_person", number=80,
            attribution="阿明", evidence_text="小林日料 人均 80",
        )
        corpus.claim(
            entity, source, run, predicate="price_per_person", number=95,
            attribution="小红", evidence_text="小林日料 人均 95",
        )
        query = "帮我找徐汇区人均100以下的日料"
        result, citations = _structured(session, query)
        answer = AnswerGenerator().generate(
            query, result, citations, classify_scope(query)
        )
        assert _uncited(result, citations) == [], "this fixture must fit the budget"
        assert answer.conflicts, "a genuine two-value conflict must still be reported"
        assert "80" in answer.content and "95" in answer.content


# ================================================== I/J. excerpts and no-result


class TestExcerptsRemainCitationBacked:
    """I: excerpts already skipped uncited chunks. This pins it against regression."""

    def test_every_excerpt_line_has_a_marker(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        excerpt_lines = _section(answer.content, "相关原文：")
        assert excerpt_lines, "fixture must produce excerpts"
        for line in excerpt_lines:
            assert _markers(line), f"excerpt with no provenance: {line!r}"

    def test_no_uncited_chunk_text_is_excerpted(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        excerpts = "\n".join(_section(answer.content, "相关原文："))
        uncited_chunks = [
            c for c in result.chunks if not citations.can_cite_chunk(c.chunk_id)
        ]
        for chunk in uncited_chunks:
            assert chunk.text not in excerpts


class TestNoResultBehaviorUnchanged:
    """J: the empty case must behave exactly as before."""

    def test_personal_no_result_still_fails_closed(self, session, corpus) -> None:
        Roadmap(corpus, ROADMAP)
        query = "我收藏里的潜水装备多少钱"
        result, citations = _retrieve(session, query, limit=4)
        assert result.is_empty()
        decision = classify_scope(query)
        assert decision.scope == "personal_required"
        chat = ScriptedChat("潜水装备一般三千元左右。")
        answer = AnswerGenerator(chat_model=chat).generate(
            query, result, citations, decision
        )
        assert answer.has_evidence is False
        assert answer.citations == []
        assert "三千元" not in answer.content
        assert chat.prompts == []

    def test_system_statements_do_not_need_citations(self, session, corpus) -> None:
        """The source-count line is system-derived, not a creator fact.

        A fix that demanded a citation for every user-visible sentence would have deleted
        this line or, worse, attached someone's evidence to it.
        """
        Roadmap(corpus, ROADMAP)
        result, citations = _retrieve(session, ROADMAP_QUERY)
        answer = AnswerGenerator().generate(
            ROADMAP_QUERY, result, citations, classify_scope(ROADMAP_QUERY)
        )
        assert "在你的收藏里找到" in answer.content
        header = next(
            line for line in answer.content.splitlines() if "在你的收藏里找到" in line
        )
        assert not _markers(header), (
            "a system diagnostic must not carry creator provenance"
        )
