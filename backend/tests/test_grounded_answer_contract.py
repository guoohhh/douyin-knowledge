"""Stage 3C blocker: a personal-collection answer must fail closed.

Three real failures from Stage 3C execution, each with its own section below.

**A — citation completeness.** The model emitted `[第五步]`, a bracket-shaped token that
is not a citation at all. `_validate_citation_markers` only understood `[n]`, so a
non-numeric label passed straight through into a grounded answer and read like
provenance. Worse, a second real answer carried several substantive factual sentences
with *no* marker anywhere, and that was accepted as a normal cited answer. Validating
"every emitted number is a real ordinal" is not the same as validating "every assertion
has support", and only the second one is the contract.

**B — same-source context.** The 手抓饼 answer said the 芹菜 evidence was "unrelated to
the 手抓饼 video" when claim, evidence and provenance link were all on that one source.
The evidence was individually valid and presented as a flat interleaved list, so the
model had no way to see which snippets shared a video. Grouping is presentation, not
provenance: `CitationBuilder` still mints every marker.

**C — evidence budget.** With `--limit 4`, title and caption chunks filled the budget and
pushed out the ASR chunk holding 八元 — the evidence that actually answered the question.
It was indexed and searchable; it simply lost to redundant metadata from its own source.

Every test here asserts on what the *user is handed* -- the rendered answer text, its
`has_evidence` flag, its citation list, or the prompt the model was actually shown. An
assertion on the validator's return value alone would have passed against the broken code
in two of these three cases.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.ai.providers import ChatMessage, ChatResponse
from douyin_knowledge.conversation.answer_generator import AnswerGenerator
from douyin_knowledge.conversation.citation_builder import CitationBuilder, CitationSet
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    ProcessingRun,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.retrieval.retriever import HybridRetriever, RetrievalResult
from tests.test_structured_retrieval import Corpus, _index


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


class Media:
    """One source carrying chunks of explicit kinds, each separately citable.

    ``Corpus.chunk`` hardcodes ``chunk_type="paragraph"`` and links *every* evidence unit
    of the source to the chunk, which cannot express "this is the caption chunk and that is
    the ASR chunk" -- the distinction blocker C is entirely about. Here each chunk gets its
    own evidence unit of the matching kind, so the chunk is citable, its kind is
    unambiguous, and two chunks of one source do not collapse into one citation.
    """

    def __init__(self, corpus: Corpus, name: str) -> None:
        self.corpus = corpus
        self.session = corpus.session
        self.source: Source
        self.run: ProcessingRun
        self.source, self.run = corpus.source(name)
        self._n = 0
        self._evidence: dict[str, EvidenceUnit] = {}

    def chunk(
        self, text: str, *, kind: str, start_ms: int | None = None
    ) -> RetrievalChunk:
        self._n += 1
        evidence = EvidenceUnit(
            source_id=self.source.id,
            kind=kind,
            start_ms=start_ms,
            end_ms=None if start_ms is None else start_ms + 3000,
            raw_text=text,
            normalized_text=text,
            content_hash=f"ev_{self.run.id}_{kind}_{self._n}",
        )
        self.session.add(evidence)
        self.session.flush()

        chunk = RetrievalChunk(
            source_id=self.source.id,
            processing_run_id=self.run.id,
            chunk_type=kind,
            ordinal=self._n,
            text=text,
            start_ms=start_ms,
            end_ms=None if start_ms is None else start_ms + 3000,
            content_hash=f"ch_{self.run.id}_{kind}_{self._n}",
        )
        self.session.add(chunk)
        self.session.flush()
        self.session.add(
            RetrievalChunkEvidence(retrieval_chunk_id=chunk.id, evidence_id=evidence.id)
        )
        self.session.flush()
        self._evidence[chunk.id] = evidence
        return chunk

    def claim(self, chunk: RetrievalChunk, **kwargs: object) -> Claim:
        """A claim resting on the evidence behind `chunk`, so retrieval can reach it.

        `_claims_for_chunks` joins claims to chunks through shared evidence, not through
        shared source: a claim is relevant because this retrieval surfaced the evidence it
        rests on. A claim minted over its own private evidence unit is invisible here, which
        is correct behaviour and the reason this helper exists.
        """
        entity = self.corpus.entity(kwargs.pop("entity_name", "老张手抓饼"))  # type: ignore[arg-type]
        claim = self.corpus.claim(
            entity, self.source, self.run, **kwargs  # type: ignore[arg-type]
        )
        self.session.add(
            ClaimEvidence(
                claim_id=claim.id,
                evidence_id=self._evidence[chunk.id].id,
                support_role="supports",
            )
        )
        self.session.flush()
        return claim


class ScriptedChat:
    """A chat model returning exactly what the test tells it to.

    These tests are about the *validator* and the *prompt*, so the model side must be
    deterministic. The recorded prompt is what section B asserts on.
    """

    def __init__(self, reply: str, *, model: str = "scripted") -> None:
        self.reply = reply
        self.model = model
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        for message in messages:
            if message.role == "user":
                self.prompts.append(message.content)
            elif message.role == "system":
                self.systems.append(message.content)
        return ChatResponse(content=self.reply, model=self.model)

    @property
    def prompt(self) -> str:
        assert self.prompts, "model was never called"
        return self.prompts[-1]

    @property
    def system(self) -> str:
        assert self.systems, "model was never called"
        return self.systems[-1]


def _retrieve(
    session: Session, query: str, *, limit: int = 10
) -> tuple[RetrievalResult, CitationSet]:
    """Real retrieval over the indexed fixture, plus the citations built from it.

    Vector search is off: `MockEmbeddingModel` would add a second ranked list whose order
    is an artifact of the mock, and every property under test here is about how a *given*
    ranked set is budgeted and presented.
    """
    _index(session)
    result = HybridRetriever(session).retrieve(
        query, limit=limit, use_vector=False, include_claims=True
    )
    return result, CitationBuilder(session).build(result)


def _answer(
    session: Session,
    query: str,
    reply: str,
    *,
    limit: int = 10,
) -> tuple[object, ScriptedChat, RetrievalResult, CitationSet]:
    """Retrieve real evidence, then let ``reply`` stand in for the model's output."""
    result, citations = _retrieve(session, query, limit=limit)
    assert result.chunks, "fixture must retrieve something or the test proves nothing"
    assert len(citations) >= 1, "fixture must be citable or the contract is vacuous"

    chat = ScriptedChat(reply)
    generator = AnswerGenerator(chat_model=chat)
    answer = generator.generate(query, result, citations, classify_scope(query))
    return answer, chat, result, citations


def _shop(corpus: Corpus, name: str = "老张手抓饼") -> Media:
    """A source whose ASR says the price, with its own title and caption chunks.

    Deliberately shaped like the real 手抓饼 case: the metadata restates the video's
    packaging, and the answer to the question exists only in the spoken content.
    """
    media = Media(corpus, f"{name} 探店")
    media.chunk(f"{name} 手抓饼 好吃", kind="title")
    media.chunk(f"{name} 手抓饼 推荐", kind="caption")
    media.chunk("一份手抓饼八元 加蛋十元", kind="asr", start_ms=12000)
    return media


# ============================================================ A. citation contract


class TestPseudoCitationsCannotSurvive:
    """`[第五步]` is bracket-shaped text, not provenance, and must not be returned.

    The old validator matched `\\[(\\d+)\\]`, so a non-numeric bracket was never even a
    candidate for validation -- it was ordinary prose that happened to render exactly like
    a citation. A regex for `[第五步]` specifically would not fix this: the failure is that
    *any* bracket which is not a citation reads as one, and the next model will invent a
    different label.
    """

    def test_pseudo_citation_does_not_reach_the_user(self, session, corpus) -> None:
        _shop(corpus)
        answer, _, _, _ = _answer(
            session,
            "手抓饼多少钱",
            "先把饼放平[第五步]，一份手抓饼八元[第五步]。",
        )
        assert "[第五步]" not in answer.content
        assert answer.generator == "deterministic_fallback"
        assert answer.diagnostics["rejected_model_answer"] is True

    def test_violation_is_reported_as_a_pseudo_citation(self, session, corpus) -> None:
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[第五步]。"
        )
        kinds = {v["kind"] for v in answer.diagnostics["grounding_violations"]}
        assert "pseudo_citation" in kinds

    @pytest.mark.parametrize("label", ["[第五步]", "[来源]", "[证据1-2]", "[]", "[n]"])
    def test_any_non_numeric_bracket_is_refused(self, session, corpus, label) -> None:
        """Not a `[第五步]` regex: the shape is the problem, not that one string."""
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", f"一份手抓饼八元{label}。"
        )
        assert answer.generator == "deterministic_fallback", label
        assert label not in answer.content, label


class TestUncitedAssertionsAreRefused:
    """A valid marker on one sentence does not license an uncited sentence beside it.

    This is the second real Stage 3C answer, and the more dangerous one: every marker it
    emitted was real, so "all markers are valid" was true while "all assertions are
    supported" was false. Nothing in the old check could tell those apart.
    """

    def test_one_cited_and_one_uncited_sentence_fails_closed(
        self, session, corpus
    ) -> None:
        _shop(corpus)
        answer, _, _, _ = _answer(
            session,
            "手抓饼多少钱",
            "一份手抓饼八元[1]。这家店晚上十点关门。",
        )
        assert answer.generator == "deterministic_fallback"
        assert "这家店晚上十点关门" not in answer.content, (
            "the unsupported sentence must not survive into the answer"
        )
        kinds = {v["kind"] for v in answer.diagnostics["grounding_violations"]}
        assert "uncited_assertion" in kinds

    def test_entirely_uncited_answer_fails_closed(self, session, corpus) -> None:
        """The whole answer is plausible prose with no provenance anywhere."""
        _shop(corpus)
        answer, _, _, _ = _answer(
            session,
            "手抓饼多少钱",
            "这家店的手抓饼很便宜，学生党可以放心去，老板态度也不错。",
        )
        assert answer.generator == "deterministic_fallback"
        assert "学生党可以放心去" not in answer.content

    def test_hedging_without_citations_is_allowed(self, session, corpus) -> None:
        """The system prompt *orders* the model to say 证据不足, so that cannot be a violation.

        A contract that rejected its own instruction would fail closed on every honest
        answer and quietly train the next prompt author to drop rule 3.
        """
        _shop(corpus)
        answer, _, _, _ = _answer(
            session,
            "手抓饼店几点关门",
            "证据里没有提到营业时间，所以我不确定。",
        )
        assert answer.generator == "model"
        assert answer.diagnostics["grounding_violations"] == []

    def test_a_hedge_bolted_onto_a_claim_is_still_refused(
        self, session, corpus
    ) -> None:
        """The exemption is anchored at sentence start so it cannot launder an assertion."""
        _shop(corpus)
        answer, _, _, _ = _answer(
            session,
            "手抓饼多少钱",
            "这家店晚上十点关门，不过证据不足。",
        )
        assert answer.generator == "deterministic_fallback"


class TestValidAnswersStillWork:
    """The contract must not be a blanket refusal; a properly cited answer passes through."""

    def test_fully_cited_answer_is_returned_as_the_model_wrote_it(
        self, session, corpus
    ) -> None:
        _shop(corpus)
        reply = "一份手抓饼八元[1]。加蛋是十元[2]。"
        answer, _, _, citations = _answer(session, "手抓饼多少钱", reply)
        assert len(citations) >= 2, "fixture must supply at least two ordinals"
        assert answer.generator == "model"
        assert answer.content == reply
        assert answer.has_evidence is True
        assert answer.diagnostics["grounding_violations"] == []

    def test_multi_ordinal_marker_is_accepted(self, session, corpus) -> None:
        """`[1,2]` is real citation syntax a model reaches for, not a malformed bracket."""
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", "手抓饼八元，加蛋十元[1,2]。"
        )
        assert answer.generator == "model"

    def test_full_width_brackets_are_not_treated_as_citations(
        self, session, corpus
    ) -> None:
        """【】 is Chinese emphasis punctuation; failing on it would reject valid answers."""
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", "【价格】一份手抓饼八元[1]。"
        )
        assert answer.generator == "model"


class TestInvalidOrdinalsRemainRejected:
    """The property the old check *did* have must survive the rewrite."""

    def test_unknown_ordinal_is_refused_and_reported(self, session, corpus) -> None:
        _shop(corpus)
        answer, _, _, citations = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]，另外还有分店[99]。"
        )
        assert 99 not in {c.ordinal for c in citations.citations}
        assert "[99]" not in answer.content
        assert answer.generator == "deterministic_fallback"
        assert answer.diagnostics["stripped_markers"] == [99]
        kinds = {v["kind"] for v in answer.diagnostics["grounding_violations"]}
        assert "unknown_ordinal" in kinds

    def test_stripping_alone_would_not_have_been_enough(self, session, corpus) -> None:
        """Why the remedy changed from repair to refusal.

        Deleting `[99]` from 还有分店[99] leaves 还有分店 -- the same unsupported assertion,
        now with the one visible sign of its unsupportedness removed. The old code returned
        exactly that as a normal grounded answer.
        """
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]，另外还有分店[99]。"
        )
        assert "还有分店" not in answer.content


class TestFallbackContainsOnlyGroundedMaterial:
    """What the user gets instead of the refused prose must itself be clean."""

    def test_fallback_carries_only_valid_markers(self, session, corpus) -> None:
        _shop(corpus)
        answer, _, _, citations = _answer(
            session,
            "手抓饼多少钱",
            "一份手抓饼八元[第五步]，还有分店[99]，另外晚上十点关门。",
        )
        import re

        valid = {c.ordinal for c in citations.citations}
        found = {int(m) for m in re.findall(r"\[(\d+)\]", answer.content)}
        assert found, "a grounded fallback must still cite its material"
        assert found <= valid
        assert not re.findall(r"\[(?!\d+(?:\s*[,，、]\s*\d+)*\])[^\[\]]*\]", answer.content)

    def test_fallback_asserts_nothing_the_model_invented(
        self, session, corpus
    ) -> None:
        """The fallback is composed from claims and evidence, so model prose cannot leak."""
        _shop(corpus)
        invented = "这家店在旺角开了二十年"
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", f"{invented}[第五步]。"
        )
        assert invented not in answer.content
        assert answer.generator == "deterministic_fallback"

    def test_fallback_still_reports_evidence_it_actually_has(
        self, session, corpus
    ) -> None:
        _shop(corpus)
        answer, _, _, _ = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[第五步]。"
        )
        assert answer.has_evidence is True
        assert answer.citations
        assert "八元" in answer.content, (
            "the grounded material that answers the question must survive the refusal"
        )


# ========================================================= B. same-source context


def _one_video_two_topics(corpus: Corpus) -> Media:
    """One video whose ASR covers two subjects -- the real 手抓饼/芹菜 shape.

    Both segments are the same creator in the same video seconds apart. Presented as a flat
    list they are indistinguishable from two unrelated videos, which is precisely the
    mistake the Stage 3C answer made.
    """
    media = Media(corpus, "老张手抓饼 探店")
    media.chunk("老张手抓饼 手抓饼 探店", kind="title")
    media.chunk("一份手抓饼八元 加蛋十元", kind="asr", start_ms=12000)
    media.chunk("配的芹菜是老板自己腌的 很爽口", kind="asr", start_ms=48000)
    return media


class TestSameSourceEvidenceIsGroupedExplicitly:
    """The prompt must state which snippets share a video, because the model cannot infer it.

    These assert on the prompt the model was *actually handed*, not on a formatting helper.
    The 手抓饼 failure was invisible to any test of the citation structure: provenance was
    correct on every row, and the answer was still wrong, because correctness of the data
    and legibility of the presentation are different properties.
    """

    def test_chunks_of_one_source_appear_under_one_source_block(
        self, session, corpus
    ) -> None:
        _one_video_two_topics(corpus)
        _, chat, result, _ = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]。"
        )
        assert len({c.source_id for c in result.chunks}) == 1, (
            "fixture must retrieve several chunks of ONE source"
        )
        assert len(result.chunks) >= 2
        assert chat.prompt.count("来源 1：") == 1
        assert "来源 2：" not in chat.prompt, (
            "one video must not be presented as two sources"
        )

    def test_both_topics_are_shown_as_one_video(self, session, corpus) -> None:
        """The 手抓饼 regression: 芹菜 and 八元 are the same video, and must read that way.

        The query reaches both segments, which is what made the production answer possible:
        the model had the 芹菜 evidence in hand and called it unrelated to the video it came
        from.
        """
        _one_video_two_topics(corpus)
        _, chat, result, _ = _answer(
            session, "手抓饼和芹菜", "一份手抓饼八元[1]，芹菜是老板自己腌的[2]。"
        )
        assert len({c.source_id for c in result.chunks}) == 1
        prompt = chat.prompt
        assert "八元" in prompt and "芹菜" in prompt
        head, _, tail = prompt.partition("来源 1：")
        assert "八元" in tail and "芹菜" in tail
        # Both sit inside the single source block, so there is no reading of this prompt on
        # which they came from different videos.
        assert "来源 2" not in tail

    def test_source_identity_is_explicit(self, session, corpus) -> None:
        _one_video_two_topics(corpus)
        _, chat, result, _ = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]。"
        )
        source_id = result.chunks[0].source_id
        assert f"source_id={source_id}" in chat.prompt
        assert "老张手抓饼" in chat.prompt

    def test_evidence_kind_and_timestamp_are_stated(self, session, corpus) -> None:
        """Kind and time are what let the model weigh a caption against what was said."""
        _one_video_two_topics(corpus)
        _, chat, _, _ = _answer(session, "手抓饼多少钱", "一份手抓饼八元[1]。")
        assert "语音转写" in chat.prompt
        assert "视频标题" in chat.prompt
        assert "00:12" in chat.prompt, "ASR evidence must carry its timestamp"

    def test_system_prompt_forbids_splitting_one_source(
        self, session, corpus
    ) -> None:
        _one_video_two_topics(corpus)
        _, chat, _, _ = _answer(session, "手抓饼多少钱", "一份手抓饼八元[1]。")
        assert "同一个视频" in chat.system

    def test_claim_provenance_travels_with_the_claim(self, session, corpus) -> None:
        """Attribution and opinion-vs-statement must survive into the prompt.

        Stage 3C accepted `creator_opinion` preservation as working; this pins that the
        grouped presentation did not drop it on the way through.
        """
        media = Media(corpus, "老张手抓饼 探店")
        media.chunk("老张手抓饼 手抓饼 探店", kind="title")
        priced = media.chunk("一份手抓饼八元", kind="asr", start_ms=12000)
        media.claim(
            priced,
            predicate="price_per_person",
            number=8,
            attribution="老张",
            evidence_text="一份手抓饼八元",
        )
        _, chat, result, _ = _answer(session, "手抓饼多少钱", "一份手抓饼八元[1]。")
        assert result.claims, "fixture must retrieve a claim"
        assert "结构化陈述" in chat.prompt
        assert "老张" in chat.prompt
        assert "作者陈述" in chat.prompt


class TestGroupingPreservesCitationProvenance:
    """Grouping is presentation only. Every marker still comes from `CitationBuilder`."""

    def test_prompt_markers_are_exactly_the_builder_ordinals(
        self, session, corpus
    ) -> None:
        import re

        _one_video_two_topics(corpus)
        _, chat, _, citations = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]。"
        )
        supplied = {c.ordinal for c in citations.citations}
        in_prompt = {int(m) for m in re.findall(r"\[(\d+)\]", chat.prompt)}
        assert in_prompt, "the prompt must offer the model real ordinals"
        assert in_prompt <= supplied, "the prompt must not invent an ordinal"

    def test_every_prompt_ordinal_resolves_to_real_provenance(
        self, session, corpus
    ) -> None:
        _one_video_two_topics(corpus)
        _, _, _, citations = _answer(
            session, "手抓饼多少钱", "一份手抓饼八元[1]。"
        )
        for citation in citations.citations:
            assert citation.source_id is not None
            assert citation.precision in {
                "source",
                "evidence",
                "evidence_timestamp",
                "claim",
            }


# ======================================================== C. retrieval budget


def _crowded(corpus: Corpus) -> Media:
    """The real 手抓饼 budget failure: redundant metadata outranking the answer.

    The ASR segment is *longer* than the caption chunks, so BM25 length normalization
    scores it below every one of them even though it is the only chunk that answers the
    question. Nothing here is contrived to make metadata win -- it wins on the real scoring
    function, which is why `--limit 4` lost the answer in production.
    """
    media = Media(corpus, "老张手抓饼 探店")
    media.chunk("老张手抓饼", kind="title")
    media.chunk("手抓饼 好吃 推荐", kind="caption")
    media.chunk("手抓饼 探店 打卡", kind="caption")
    media.chunk("手抓饼 学生党 必吃", kind="caption")
    media.chunk(
        "老板说一份手抓饼八元 加个蛋是十元 这个价格在这一片算便宜的了",
        kind="asr",
        start_ms=12000,
    )
    return media


def _types(result: RetrievalResult) -> list[str]:
    return [c.chunk_type for c in result.chunks]


def _has_answer(result: RetrievalResult) -> bool:
    return any("八元" in c.text for c in result.chunks)


class TestRelevantEvidenceSurvivesTheBudget:
    """`ask --limit 4` must not lose the only chunk that answers the question."""

    def test_asr_evidence_is_not_crowded_out_by_same_source_metadata(
        self, session, corpus
    ) -> None:
        _crowded(corpus)
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        assert len(result.chunks) == 4
        assert _has_answer(result), (
            f"the 八元 ASR chunk must survive limit=4; got {_types(result)}"
        )

    def test_one_source_does_not_spend_the_budget_on_metadata(
        self, session, corpus
    ) -> None:
        """At most one metadata chunk per source wins a slot outright."""
        _crowded(corpus)
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        metadata = [t for t in _types(result) if t in {"title", "caption"}]
        assert len(metadata) < 4, f"metadata filled the whole budget: {_types(result)}"

    def test_the_answer_reaches_the_user(self, session, corpus) -> None:
        """The budget is only fixed if the rendered answer carries the evidence.

        A retrieval-level assertion would have passed while the answer still omitted 八元,
        which is the same gap that let the structured rejection bug ship.
        """
        _crowded(corpus)
        result, citations = _retrieve(session, "手抓饼多少钱", limit=4)
        answer = AnswerGenerator().generate(
            "手抓饼多少钱", result, citations, classify_scope("手抓饼多少钱")
        )
        assert "八元" in answer.content

    def test_larger_limit_is_not_what_fixed_it(self, session, corpus) -> None:
        """The limit the user asked for is the limit they get."""
        _crowded(corpus)
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        assert len(result.chunks) == 4


class TestMetadataRemainsSearchable:
    """Metadata is deferred behind distinct evidence, never disabled."""

    def test_a_title_query_still_retrieves_the_title(self, session, corpus) -> None:
        _crowded(corpus)
        result, _ = _retrieve(session, "老张", limit=4)
        assert "title" in _types(result)

    def test_a_caption_query_still_retrieves_the_caption(
        self, session, corpus
    ) -> None:
        _crowded(corpus)
        result, _ = _retrieve(session, "学生党 必吃", limit=4)
        assert "caption" in _types(result)

    def test_deferred_metadata_is_returned_when_budget_allows(
        self, session, corpus
    ) -> None:
        """Deferral is not exclusion: with room, every chunk still comes back."""
        _crowded(corpus)
        result, _ = _retrieve(session, "手抓饼多少钱", limit=10)
        assert _types(result).count("caption") == 3
        assert "title" in _types(result)
        assert _has_answer(result)

    def test_metadata_can_still_outrank_content(self, session, corpus) -> None:
        """The first metadata chunk of a source is never deferred."""
        _crowded(corpus)
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        assert _types(result)[0] == "title"


class TestBudgetDoesNotWeakenFilters:
    """The budget reorders the candidate set; it cannot enlarge it."""

    def test_excluded_source_stays_unreachable(self, session, corpus) -> None:
        media = Media(corpus, "老张手抓饼 探店")
        media.chunk("老张手抓饼", kind="title")
        media.chunk("一份手抓饼八元", kind="asr", start_ms=12000)
        state = session.get(SourceProcessingState, media.source.id)
        assert state is not None
        state.current_policy_action = "exclude"
        session.flush()
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        assert result.chunks == []

    def test_superseded_run_stays_unreachable(self, session, corpus) -> None:
        media = Media(corpus, "老张手抓饼 探店")
        media.chunk("老张手抓饼", kind="title")
        media.chunk("一份手抓饼八元", kind="asr", start_ms=12000)
        corpus.supersede(media.source, corpus.run(media.source))
        result, _ = _retrieve(session, "手抓饼多少钱", limit=4)
        assert result.chunks == []


# ==================================================== 10. no-result personal query


class TestNoResultPersonalQueryFailsClosed:
    """An empty personal retrieval must not become a general-knowledge answer.

    This is the invariant the whole change exists to protect, stated at the outer boundary:
    with a chat model configured and willing to answer, a personal question with no
    supporting evidence still gets 没找到 and nothing else.
    """

    def test_no_evidence_no_citations_no_general_fallback(
        self, session, corpus
    ) -> None:
        _shop(corpus)
        query = "我收藏里的潜水装备多少钱"
        result, citations = _retrieve(session, query, limit=4)
        assert result.is_empty(), "fixture must genuinely retrieve nothing"

        decision = classify_scope(query)
        assert decision.scope == "personal_required"

        chat = ScriptedChat("潜水装备一般在三千元左右。")
        answer = AnswerGenerator(chat_model=chat).generate(
            query, result, citations, decision
        )
        assert answer.has_evidence is False
        assert answer.citations == []
        assert "三千元" not in answer.content, (
            "general knowledge must not fill a personal no-result"
        )
        assert "没有在你已经处理的收藏里找到足够证据" in answer.content
        assert chat.prompts == [], "the model must not even be consulted"

