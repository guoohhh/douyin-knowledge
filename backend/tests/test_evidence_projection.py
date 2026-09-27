"""Stage 3C: a retrieved chunk must project the evidence that answers the question.

Retrieval was never the problem. On the real 手抓饼 corpus the right chunk won:

    chk_a0e1bbdfed0f9861e90ba2  rank 1  chunk_type=asr  56 linked EvidenceUnits
    span 00:00.040 - 02:22.230
    text: ... 手抓饼。/ 多少钱？/ 八元。/ 八元？ ...

and then `CitationBuilder` projected all 56 units of it onto one:

    ev_a0e1b8d61205159f8c2bda  @ 01:55  补钙啊？

`_build_prompt` renders ``citation.snippet or chunk.text``, so the model was handed

    [1]（语音转写 @ 01:55）补钙啊？

in answer to 手抓饼多少钱. The 八元 that the retriever had correctly found disappeared
between retrieval and generation.

The type error underneath is ``RetrievalChunk != EvidenceUnit``. A chunk is a retrieval
context and may legitimately hold many units; a Citation is a provenance pointer to one. The
old code collapsed the first into the second and then rendered the survivor as if it stood
for the whole chunk -- which is how a neighbouring line's timestamp came to be offered as
proof of an unrelated claim.

Three fixes that are not this fix, and why:

*``citation.snippet`` -> ``chunk.text``.* The model would see 八元 and could write 八元[1],
but ``[1]`` still resolves to 补钙啊？. Fake semantic provenance, now with the answer
visible so the user is less likely to check.

*Earliest unit by timestamp.* Deterministic, and wrong: the first thing said in a video is
not the answer to the question. Here it is 大家好啊今天带大家来逛逛这个夜市.

*Unit with maximum query overlap.* For 手抓饼多少钱 that is 手抓饼。 or 多少钱？ -- the
restatement of the question. The answer is the next unit, 八元。, which shares no token with
the query at all. Overlap can only locate the anchor; the window is what holds the answer.

So: a chunk projects a small, bounded, deterministic window of its own evidence, and every
unit in that window gets its own citation with its own timestamp.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.answer_generator import AnswerGenerator
from douyin_knowledge.conversation.citation_builder import (
    MAX_EVIDENCE_PER_CHUNK,
    CitationBuilder,
    CitationSet,
)
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    ProcessingRun,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.retrieval.retriever import HybridRetriever, RetrievalResult
from tests.test_structured_retrieval import Corpus, _index

QUERY = "手抓饼多少钱"

#: The real span: 00:00.040 - 02:22.230.
SPAN_START = 40
SPAN_END = 142_230

#: One ASR pass over a night-market walkthrough, in spoken order. The answer-bearing passage
#: is 手抓饼。/ 多少钱？/ 八元。 near the end; 补钙啊？ is the distant unit the old projection
#: actually cited; and 多少钱一份 early on is a *second* price question about a different
#: dish, which is what makes "highest single overlap" insufficient rather than merely
#: fragile.
TRANSCRIPT = [
    "大家好啊今天带大家来逛逛这个夜市",
    "这边是刚开的一条小吃街",
    "人特别多啊",
    "先看看这家",
    "老板这个是什么",
    "这是烤冷面",
    "多少钱一份",
    "十块钱",
    "行来一份",
    "旁边这家在卖什么呢",
    "是炸串",
    "炸串这边是一块五一串",
    "有点贵啊",
    "再往前走走",
    "这个味道好香",
    "是烤鱿鱼",
    "老板鱿鱼怎么卖",
    "大的二十小的十二",
    "我们要个小的",
    "继续往里面走",
    "这边人更多了",
    "都在排队",
    "排的是什么队",
    "好像是奶茶",
    "奶茶就不喝了",
    "我们看看别的",
    "这边有卖水果的",
    "西瓜切好装盒的",
    "一盒五块",
    "还挺划算",
    "旁边是卖凉皮的",
    "凉皮八块一碗",
    "这个我也想吃",
    "老板给我来一碗",
    "你这个加了什么啊 补钙啊？",
    "哈哈哈老板真幽默",
    "我们继续逛",
    "前面那家排队的",
    "闻起来是饼的味道",
    "走过去看看",
    "手抓饼。",
    "多少钱？",
    "八元。",
    "八元？",
    "有点小贵但是看起来真的很好吃",
    "老板加个鸡蛋加个里脊",
    "那样是多少",
    "加料十二",
    "行就要那个",
    "等一下就好",
    "看这个饼做得真厚实",
    "拿到手了先拍个照",
    "咬一口太香了",
    "这一趟没白来",
    "下次还来这条街",
    "今天就到这里拜拜",
]

ANSWER = "八元。"
QUESTION = "多少钱？"
DISH = "手抓饼。"
DISTANT = "你这个加了什么啊 补钙啊？"


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


class NightMarket:
    """One ASR chunk over one long video, linking every transcript line as evidence.

    The real shape, and the point of the fixture: 56 units, a 2m22s span, one chunk. The
    old simplified fixtures gave each unit its own chunk, which is exactly the case where
    projecting a chunk to one unit is correct -- so they could not have caught this.

    ``first`` forces one line to sort first by id, which is what the unordered join used to
    surface. ``shuffle`` controls the order rows are *written* in. Insertion order must not reach the
    user, and the old code let it: with no ``ORDER BY``, ``chunk.evidence_ids`` came back in
    whatever order SQLite chose, the projection took the first timestamped unit it saw, and
    the same corpus cited a different moment depending on how it had been loaded.
    """

    def __init__(
        self,
        corpus: Corpus,
        lines: list[str] = TRANSCRIPT,
        *,
        shuffle: int | None = None,
        first: str | None = None,
    ) -> None:
        self.session = corpus.session
        self.source = Source(
            platform="douyin",
            external_id="s_night_market",
            source_type="video",
            title="夜市小吃街全程记录",
        )
        self.session.add(self.source)
        self.session.flush()

        self.run = ProcessingRun(
            source_id=self.source.id,
            run_kind="full",
            schema_version="1.0",
            processor_version="0.1.0",
            status="succeeded",
            target_level=2,
            achieved_level=2,
            started_at_ms=now_ms(),
            finished_at_ms=now_ms(),
        )
        self.session.add(self.run)
        self.session.flush()
        self.session.add(
            SourceProcessingState(
                source_id=self.source.id,
                current_policy_action="process",
                processing_status="succeeded",
                current_processing_run_id=self.run.id,
            )
        )
        self.session.flush()

        self.chunk = RetrievalChunk(
            source_id=self.source.id,
            processing_run_id=self.run.id,
            chunk_type="asr",
            ordinal=0,
            text=" / ".join(lines),
            start_ms=SPAN_START,
            end_ms=SPAN_END,
            content_hash="c_night_market",
        )
        self.session.add(self.chunk)
        self.session.flush()

        step = (SPAN_END - SPAN_START) // len(lines)
        self.units: dict[str, EvidenceUnit] = {}
        pending: list[EvidenceUnit] = []
        for index, text in enumerate(lines):
            start_ms = SPAN_START + index * step
            unit = EvidenceUnit(
                source_id=self.source.id,
                kind="asr",
                start_ms=start_ms,
                end_ms=start_ms + step - 10,
                raw_text=text,
                normalized_text=text,
                content_hash=f"h_night_market_{index}",
            )
            pending.append(unit)
            self.units[text] = unit

        if shuffle is not None:
            random.Random(shuffle).shuffle(pending)
        if first is not None:
            # Reproduces the reported failure exactly. The unordered join returned rows in
            # primary-key order, and the projection took the first timestamped one it saw,
            # so a low-sorting id is what made ev_a0e1b8d61205159f8c2bda @ 01:55 the
            # citation for 手抓饼多少钱. Forcing the id here rather than the insert order
            # because the insert order was never what decided it -- that is the bug.
            head = next(u for u in pending if (u.normalized_text or "") == first)
            head.id = "ev_000000000000000000000f"
            pending.remove(head)
            pending.insert(0, head)
        for unit in pending:
            self.session.add(unit)
        self.session.flush()
        for unit in pending:
            self.session.add(
                RetrievalChunkEvidence(
                    retrieval_chunk_id=self.chunk.id, evidence_id=unit.id
                )
            )
        self.session.flush()

    def id_of(self, text: str) -> str:
        return self.units[text].id


def _retrieve(
    session: Session, query: str = QUERY, *, limit: int = 20
) -> tuple[RetrievalResult, CitationSet]:
    _index(session)
    result = HybridRetriever(session).retrieve(
        query, limit=limit, use_vector=False, include_claims=True
    )
    return result, CitationBuilder(session).build(result)


def _prompt(session: Session, result: RetrievalResult, citations: CitationSet) -> str:
    """The evidence block the model is actually handed."""
    return AnswerGenerator()._build_prompt(
        QUERY, result, citations, classify_scope(QUERY)
    )


def _snippets(citations: CitationSet, chunk_id: str) -> list[str]:
    return [c.snippet or "" for c in citations.citations_for_chunk(chunk_id)]


def _marker_target(citations: CitationSet, text: str) -> str | None:
    """The evidence id the citation whose snippet is `text` points at."""
    for citation in citations.citations:
        if (citation.snippet or "").strip() == text:
            return citation.evidence_id
    return None


class TestRetrievalWasAlreadyCorrect:
    """Nothing upstream of the projection is being changed, so nothing may move."""

    def test_the_asr_chunk_is_rank_one(self, session, corpus) -> None:
        market = NightMarket(corpus)
        result, _ = _retrieve(session)
        assert result.chunks[0].chunk_id == market.chunk.id

    def test_the_chunk_links_every_transcript_line(self, session, corpus) -> None:
        NightMarket(corpus)
        result, _ = _retrieve(session)
        assert len(result.chunks[0].evidence_ids) == len(TRANSCRIPT)

    def test_the_chunk_spans_the_whole_video(self, session, corpus) -> None:
        NightMarket(corpus)
        result, _ = _retrieve(session)
        assert (result.chunks[0].start_ms, result.chunks[0].end_ms) == (
            SPAN_START,
            SPAN_END,
        )

    def test_the_answer_is_in_the_retrieved_text(self, session, corpus) -> None:
        """The information was always there. Everything else here is about losing it."""
        NightMarket(corpus)
        result, _ = _retrieve(session)
        assert ANSWER in result.chunks[0].text


class TestTheAnswerReachesTheModel:
    """The failure, stated as the thing that must now be true."""

    def test_the_projection_includes_the_answer(self, session, corpus) -> None:
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        assert ANSWER in _snippets(citations, market.chunk.id)

    def test_the_model_sees_the_answer(self, session, corpus) -> None:
        NightMarket(corpus)
        result, citations = _retrieve(session)
        assert "八元" in _prompt(session, result, citations)

    def test_the_window_is_the_question_and_its_answer(self, session, corpus) -> None:
        """The whole passage, in spoken order, so 八元 has something to be the price of."""
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        assert _snippets(citations, market.chunk.id) == [DISH, QUESTION, ANSWER]

    def test_the_answer_marker_points_at_the_answer(self, session, corpus) -> None:
        """A marker on 八元 must resolve to the unit that says 八元, not to a neighbour."""
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        assert _marker_target(citations, ANSWER) == market.id_of(ANSWER)

    def test_every_projected_citation_points_at_its_own_snippet(
        self, session, corpus
    ) -> None:
        """No citation borrows another unit's text, timestamp or id."""
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        for citation in citations.citations_for_chunk(market.chunk.id):
            unit = market.units[(citation.snippet or "").strip()]
            assert citation.evidence_id == unit.id
            assert citation.start_ms == unit.start_ms
            assert citation.precision == "evidence_timestamp"

    def test_the_deterministic_answer_quotes_the_answer(self, session, corpus) -> None:
        """Not just the prompt: the fallback the user reads when the model is rejected."""
        NightMarket(corpus)
        result, citations = _retrieve(session)
        answer = AnswerGenerator().generate(
            QUERY, result, citations, classify_scope(QUERY)
        )
        assert "八元" in answer.content
        assert answer.has_evidence


class TestTheDistantEvidenceIsNotSubstituted:
    """补钙啊？ is a real unit of this chunk. It is not an answer to this question.

    Every test here writes it first, which is the insertion order that produced the reported
    failure: the baseline cited the first timestamped unit in row order. Without that,
    these would pass at the baseline by luck -- 补钙 is one arbitrary pick out of 56.
    """

    def test_the_distant_unit_is_not_projected(self, session, corpus) -> None:
        market = NightMarket(corpus, first=DISTANT)
        _, citations = _retrieve(session)
        assert DISTANT not in _snippets(citations, market.chunk.id)

    def test_the_distant_unit_is_not_in_the_prompt(self, session, corpus) -> None:
        NightMarket(corpus, first=DISTANT)
        result, citations = _retrieve(session)
        assert "补钙" not in _prompt(session, result, citations)

    def test_no_citation_points_at_the_distant_unit(self, session, corpus) -> None:
        market = NightMarket(corpus, first=DISTANT)
        _, citations = _retrieve(session)
        distant_id = market.id_of(DISTANT)
        assert all(c.evidence_id != distant_id for c in citations.citations)

    def test_the_answer_is_not_subordinated_to_the_distant_unit(
        self, session, corpus
    ) -> None:
        """The exact reported substitution: 补钙啊？ standing in for the 八元 evidence."""
        market = NightMarket(corpus, first=DISTANT)
        _, citations = _retrieve(session)
        assert _snippets(citations, market.chunk.id) == [DISH, QUESTION, ANSWER]
        assert _marker_target(citations, ANSWER) == market.id_of(ANSWER)

    def test_the_earlier_price_question_is_not_the_anchor(self, session, corpus) -> None:
        """多少钱一份 matches the query too -- about 烤冷面, forty lines earlier.

        Scoring units independently and taking the best one decides between two passages on
        a one-token margin. The window is anchored on the run where the query's terms
        converge, which is the passage the user is asking about.
        """
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        assert "十块钱" not in _snippets(citations, market.chunk.id)
        assert market.id_of("多少钱一份") not in {
            c.evidence_id for c in citations.citations
        }


class TestOrderingIsDeterministic:
    """Which moment the user is shown must not depend on how rows were written."""

    @pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 4242])
    def test_shuffled_insertion_projects_the_same_window(
        self, session, corpus, seed: int
    ) -> None:
        """Measured on the baseline this varied: 手抓饼。/ 八元？/ 走过去看看/ 加料十二/
        这个味道好香 across five seeds, one arbitrary unit each.
        """
        market = NightMarket(corpus, shuffle=seed)
        _, citations = _retrieve(session)
        assert _snippets(citations, market.chunk.id) == [DISH, QUESTION, ANSWER]

    def test_evidence_ids_arrive_in_temporal_order(self, session, corpus) -> None:
        """The retriever orders them; the projection does not depend on that, but callers
        reading `chunk.evidence_ids` directly get a stable list rather than row order.
        """
        market = NightMarket(corpus, shuffle=99)
        result, _ = _retrieve(session)
        expected = [market.id_of(text) for text in TRANSCRIPT]
        assert result.chunks[0].evidence_ids == expected

    def test_a_chunk_with_no_query_signal_cites_its_earliest_unit(
        self, session, corpus
    ) -> None:
        """No unit matches, so there is nothing to choose between and one citation is honest.

        Reachable in production when the vector arm retrieves a chunk whose transcript shares
        no token with the query. Built by rewriting the query on a real retrieval result,
        because FTS by definition cannot produce a no-overlap hit. A window here would be
        three arbitrary units instead of one; this is the pre-existing single-citation
        behavior, kept.
        """
        market = NightMarket(corpus)
        result, _ = _retrieve(session)
        result.query = "量子计算的退火算法"
        citations = CitationBuilder(session).build(result)
        window = citations.citations_for_chunk(market.chunk.id)
        assert len(window) == 1
        assert window[0].evidence_id == market.id_of(TRANSCRIPT[0])


class TestTheProjectionIsBounded:
    """A 56-unit chunk must not dump 56 units into the prompt."""

    def test_one_chunk_mints_at_most_the_window_size(self, session, corpus) -> None:
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        assert len(citations.citations_for_chunk(market.chunk.id)) <= MAX_EVIDENCE_PER_CHUNK

    def test_the_overall_budget_still_holds(self, session, corpus) -> None:
        NightMarket(corpus)
        result, _ = _retrieve(session)
        for limit in (1, 2, 3, 5, 12):
            citations = CitationBuilder(session).build(result, limit=limit)
            assert len(citations.citations) <= limit

    def test_a_tight_budget_truncates_the_window_not_the_chunk(
        self, session, corpus
    ) -> None:
        """With one slot the chunk is still citable, at the strongest anchor."""
        market = NightMarket(corpus)
        result, _ = _retrieve(session)
        citations = CitationBuilder(session).build(result, limit=1)
        window = citations.citations_for_chunk(market.chunk.id)
        assert len(window) == 1
        assert citations.can_cite_chunk(market.chunk.id)

    def test_the_prompt_evidence_block_stays_bounded(self, session, corpus) -> None:
        NightMarket(corpus)
        result, citations = _retrieve(session)
        prompt = _prompt(session, result, citations)
        evidence_lines = [line for line in prompt.splitlines() if "（语音转写" in line]
        assert 0 < len(evidence_lines) <= MAX_EVIDENCE_PER_CHUNK


class TestExistingBehaviorIsUnchanged:
    """Single-evidence chunks, claim citations and the empty case all predate this."""

    def test_a_single_evidence_chunk_still_mints_one_citation(
        self, session, corpus
    ) -> None:
        """The common shape. A chunk with one unit has no window to choose."""
        market = NightMarket(corpus, [DISH, QUESTION, ANSWER])
        single = RetrievalChunk(
            source_id=market.source.id,
            processing_run_id=market.run.id,
            chunk_type="paragraph",
            ordinal=1,
            text="手抓饼 多少钱 八元",
            content_hash="c_single",
        )
        session.add(single)
        session.flush()
        session.add(
            RetrievalChunkEvidence(
                retrieval_chunk_id=single.id, evidence_id=market.id_of(ANSWER)
            )
        )
        session.flush()

        _, citations = _retrieve(session)
        window = citations.citations_for_chunk(single.id)
        assert len(window) == 1
        assert window[0].evidence_id == market.id_of(ANSWER)
        assert window[0].precision == "evidence_timestamp"

    def test_an_untimestamped_unit_is_cited_at_evidence_precision(
        self, session, corpus
    ) -> None:
        """The precision hierarchy is untouched: no timestamp means no timestamp claimed."""
        source, run = corpus.source("图文笔记")
        unit = EvidenceUnit(
            source_id=source.id,
            kind="ocr",
            raw_text="手抓饼 八元",
            normalized_text="手抓饼 八元",
            content_hash="h_no_stamp",
        )
        session.add(unit)
        session.flush()
        chunk = RetrievalChunk(
            source_id=source.id,
            processing_run_id=run.id,
            chunk_type="paragraph",
            ordinal=0,
            text="手抓饼 八元",
            content_hash="c_no_stamp",
        )
        session.add(chunk)
        session.flush()
        session.add(
            RetrievalChunkEvidence(retrieval_chunk_id=chunk.id, evidence_id=unit.id)
        )
        session.flush()

        _, citations = _retrieve(session)
        window = citations.citations_for_chunk(chunk.id)
        assert [c.precision for c in window] == ["evidence"]
        assert window[0].start_ms is None

    def test_a_claim_still_reuses_an_already_cited_unit(self, session, corpus) -> None:
        """Claim reuse reads `evidence_to_citation`, which windows now populate per unit.

        A wider projection should make reuse *more* available, not less: the claim resting on
        八元。 attaches to that unit's citation instead of minting a second ordinal.
        """
        from douyin_knowledge.db.models.entities import ClaimEvidence

        market = NightMarket(corpus)
        entity = corpus.entity("手抓饼摊", entity_type="place")
        claim = corpus.claim(
            entity,
            market.source,
            market.run,
            predicate="price_per_person",
            number=8,
            evidence_text="手抓饼 八元",
        )
        session.add(
            ClaimEvidence(
                claim_id=claim.id,
                evidence_id=market.id_of(ANSWER),
                support_role="supports",
            )
        )
        session.flush()

        _, citations = _retrieve(session)
        assert citations.can_cite_claim(claim.id)
        reused = citations.by_claim[claim.id]
        assert reused.evidence_id == market.id_of(ANSWER)
        assert reused.claim_id == claim.id
        assert reused.precision == "evidence_timestamp"

    def test_a_shared_unit_is_not_cited_twice(self, session, corpus) -> None:
        """Two chunks over one unit share its ordinal.

        Two markers on one moment of one video reads as two independent confirmations, which
        would overstate the support -- the same reason claim reuse exists.
        """
        market = NightMarket(corpus, [DISH, QUESTION, ANSWER])
        second = RetrievalChunk(
            source_id=market.source.id,
            processing_run_id=market.run.id,
            chunk_type="paragraph",
            ordinal=1,
            text="手抓饼 多少钱 八元",
            content_hash="c_overlapping",
        )
        session.add(second)
        session.flush()
        for text in (DISH, QUESTION, ANSWER):
            session.add(
                RetrievalChunkEvidence(
                    retrieval_chunk_id=second.id, evidence_id=market.id_of(text)
                )
            )
        session.flush()

        _, citations = _retrieve(session)
        evidence_ids = [c.evidence_id for c in citations.citations]
        assert len(evidence_ids) == len(set(evidence_ids))
        assert citations.citations_for_chunk(market.chunk.id) == (
            citations.citations_for_chunk(second.id)
        )

    def test_no_result_behavior_is_unchanged(self, session, corpus) -> None:
        NightMarket(corpus)
        result, citations = _retrieve(session, "量子计算的退火算法")
        assert result.is_empty()
        assert citations.citations == []
        answer = AnswerGenerator().generate(
            "量子计算的退火算法",
            result,
            citations,
            classify_scope("量子计算的退火算法"),
        )
        assert not answer.has_evidence


class TestTheApiSaysWhatItMeans:
    """`by_chunk` promised one citation per chunk. That promise was the bug."""

    def test_by_chunk_is_gone(self, session, corpus) -> None:
        """Renamed rather than redefined: a caller written against the old single-Citation
        value would silently misread a list, and 'the chunk's citation' is exactly the
        assumption that produced the failure.
        """
        NightMarket(corpus)
        _, citations = _retrieve(session)
        assert not hasattr(citations, "by_chunk")

    def test_citations_for_chunk_is_a_copy(self, session, corpus) -> None:
        """A caller mutating the returned list must not edit the citation set."""
        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        window = citations.citations_for_chunk(market.chunk.id)
        window.clear()
        assert len(citations.citations_for_chunk(market.chunk.id)) == 3

    def test_an_unknown_chunk_has_no_window(self, session, corpus) -> None:
        NightMarket(corpus)
        _, citations = _retrieve(session)
        assert citations.citations_for_chunk("chk_nonexistent") == []
        assert not citations.can_cite_chunk("chk_nonexistent")

    def test_persisted_citations_round_trip_every_window_unit(
        self, session, corpus
    ) -> None:
        """`message_citations` already held many rows per message; nothing migrated."""
        from douyin_knowledge.db.models.conversation import Conversation, Message

        market = NightMarket(corpus)
        _, citations = _retrieve(session)
        conversation = Conversation(title="t")
        session.add(conversation)
        session.flush()
        message = Message(conversation_id=conversation.id, role="assistant", content="x")
        session.add(message)
        session.flush()

        builder = CitationBuilder(session)
        assert builder.persist(message.id, citations) == len(citations.citations)
        session.flush()

        loaded = builder.load(message.id)
        assert [row["ordinal"] for row in loaded] == [1, 2, 3]
        assert [row["snippet"] for row in loaded] == [DISH, QUESTION, ANSWER]
        assert [row["evidence_id"] for row in loaded] == [
            market.id_of(DISH),
            market.id_of(QUESTION),
            market.id_of(ANSWER),
        ]
