"""A structured match may be presented as 符合条件 only if the answer can prove it.

The previous change ([[citable-answer]]) filtered individual support and conflict claims
through the current `CitationSet`, so no constraint *line* can state a value it cannot
cite. The match header was still rendered before any of that filtering:

    在你的收藏里找到 2 个符合条件的结果。
    ...
    2. 小林日料
       - 菜系：日料（来自 阿明）[12]

That numbered line is itself an assertion -- "this entity satisfies your hard
constraints" -- and here the claim that cleared 人均 < 100 had no citation, so the price
line simply vanished. The user is told the restaurant qualifies on a price they cannot
check, and cannot even tell that a constraint went unproven. Qualifying in the executor
and being presentable as qualified are different statements:

    executor qualification != renderable qualification

So rendering requires that *every* claim-derived required constraint have at least one
citable satisfying claim. Every one, not any: a cited 菜系 line does not prove the price,
and "some citation somewhere for this entity" would let an unrelated excerpt from the same
video stand in for the constraint that actually decided qualification.

Three things stay out of that rule, and they are the reason this file traces the data model
instead of demanding provenance everywhere:

* Optional constraints (``required=False``) -- demanding provenance would promote them to
  mandatory and drop results that legitimately qualified without them.
* Entity type/subtype narrowing -- our own annotation, not a creator's assertion.
* User state (DEC-018) -- 想去 is the user's own declaration and needs no creator claim.

`StructuredResult` is never mutated: retrieval diagnostics have to keep reporting what the
executor actually found, so the withheld matches stay in `structured.matches` and only the
rendering narrows. Every assertion below is on rendered answer text or on the untouched
structured result.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.conversation.answer_generator import AnswerGenerator
from douyin_knowledge.conversation.citation_builder import CitationBuilder
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
from douyin_knowledge.db.models.processing import (
    EvidenceUnit,
    RetrievalChunk,
    RetrievalChunkEvidence,
)
from douyin_knowledge.retrieval.query_plan import (
    ClaimConstraint,
    LocationConstraint,
    QueryPlan,
    UserStateConstraint,
)
from douyin_knowledge.retrieval.retriever import HybridRetriever
from tests.test_structured_retrieval import Corpus, _index

QUERY = "帮我找徐汇区人均100以下的日料"

#: Enough chunks that this source's own claims are starved of citation slots. Chunks are
#: cited first and in retrieval order, so a match can lose provenance for the very claim
#: that qualified it without anything being wrong with the claim.
FLOOD = 20


@pytest.fixture
def corpus(session: Session) -> Corpus:
    return Corpus(session)


#: Flood size for the tests that need the starved match to be *partially* citable. Smaller
#: than `FLOOD` so that `_chunk_for_claim`'s single chunk still fits inside the retrieval
#: limit: with a full flood it ranks eleventh of ten and never reaches the citation builder,
#: which starves the match completely and destroys the precondition those tests rest on.
FLOOD_PARTIAL = 9


def _chunk_for_claim(corpus: Corpus, entity: Entity, predicate: str, text: str) -> None:
    """A chunk grounded in exactly one claim's evidence, and nothing else's.

    This is how a match ends up citable for one required constraint and not another without
    anything being wrong: the cuisine sentence got its own retrievable paragraph, the price
    sentence did not, and the budget ran out before the price claim could mint. `Corpus.chunk`
    cannot express that -- it links every evidence unit on the source, which makes all three
    claims citable at once.

    The shape used to appear by accident. `CitationBuilder` overwrote
    ``evidence_to_citation`` once per chunk while citing only one unit per chunk, so a
    flooded source left most of the budget unspent and the split fell out of the leftovers.
    Now that a chunk cites the evidence it actually resolves to, the fixture has to say what
    it means.
    """
    claim = corpus.session.scalars(
        select(Claim).where(
            Claim.subject_entity_id == entity.id, Claim.predicate == predicate
        )
    ).one()
    evidence_id = corpus.session.scalars(
        select(ClaimEvidence.evidence_id).where(ClaimEvidence.claim_id == claim.id)
    ).one()
    chunk = RetrievalChunk(
        source_id=claim.source_id,
        processing_run_id=claim.processing_run_id,
        chunk_type="paragraph",
        ordinal=99,
        text=text,
        content_hash=f"c_one_claim_{claim.id}",
    )
    corpus.session.add(chunk)
    corpus.session.flush()
    corpus.session.add(
        RetrievalChunkEvidence(retrieval_chunk_id=chunk.id, evidence_id=evidence_id)
    )
    corpus.session.flush()


def _flood(corpus: Corpus, source, run, texts: list[str]) -> None:
    """Paragraph chunks, each grounded in its own evidence unit.

    `_paragraphs` links every chunk to *all* of the source's existing evidence, which was
    fine when a chunk minted one citation regardless. Once a chunk projects a window of its
    evidence, chunk 2 onward resolve to units chunk 1 already cited and reuse those
    ordinals, so twenty chunks consumed three slots and this file's whole starvation
    precondition evaporated -- every test here failed on its own setup, not on the rule.

    A real paragraph chunk is grounded in the text of that paragraph, so that is what this
    builds. Twenty distinct units is what actually exhausts a twelve-citation budget, and it
    exhausts it the way production does.
    """
    for index, text in enumerate(texts, start=1):
        evidence = EvidenceUnit(
            source_id=source.id,
            kind="transcript",
            raw_text=text,
            normalized_text=text,
            content_hash=f"h_flood_{run.id}_{index}",
        )
        corpus.session.add(evidence)
        corpus.session.flush()
        chunk = RetrievalChunk(
            source_id=source.id,
            processing_run_id=run.id,
            chunk_type="paragraph",
            ordinal=index,
            text=text,
            content_hash=f"c_flood_{run.id}_{index}",
        )
        corpus.session.add(chunk)
        corpus.session.flush()
        corpus.session.add(
            RetrievalChunkEvidence(retrieval_chunk_id=chunk.id, evidence_id=evidence.id)
        )
        corpus.session.flush()


def _restaurant(
    corpus: Corpus,
    name: str,
    *,
    district: str = "徐汇区",
    cuisine: str = "日料",
    price: float = 90,
    flood: int = 0,
) -> Entity:
    """One qualifying restaurant, optionally with its citation budget flooded."""
    source, run = corpus.source(f"{name} 探店")
    entity = corpus.entity(name)
    entity.subtype = "restaurant"
    corpus.session.flush()
    corpus.claim(
        entity, source, run, predicate="located_in", text=district,
        evidence_text=f"{name} 在{district}",
    )
    corpus.claim(
        entity, source, run, predicate="cuisine", text=cuisine,
        evidence_text=f"{name} 是{cuisine}",
    )
    corpus.claim(
        entity, source, run, predicate="price_per_person", number=price,
        evidence_text=f"{name} 人均 {price:g}",
    )
    if flood:
        _flood(
            corpus,
            source,
            run,
            [
                f"{name} {cuisine} {district} 人均 {price:g} 探店 第{index}段"
                for index in range(flood)
            ],
        )
    return entity


def _run(session: Session, plan: QueryPlan):
    _index(session)
    result = HybridRetriever(session).retrieve_structured(plan, use_vector=False)
    return result, CitationBuilder(session).build(result)


def _parsed(session: Session, query: str = QUERY):
    from douyin_knowledge.retrieval.query_parser import parse_query

    plan, _ = parse_query(query, scope="personal_required", limit=10)
    return _run(session, plan)


def _answer(session: Session, plan: QueryPlan, query: str = QUERY):
    result, citations = _run(session, plan)
    return result, citations, AnswerGenerator().generate(
        query, result, citations, classify_scope(query)
    )


def _listed(content: str) -> list[str]:
    """Entity names actually presented as qualifying results."""
    return [m.group(1) for m in re.finditer(r"^\d+\. (.+)$", content, re.MULTILINE)]


def _count(content: str) -> int:
    match = re.search(r"在你的收藏里找到 (\d+) 个符合条件的结果", content)
    assert match is not None, f"no result-count line in: {content!r}"
    return int(match.group(1))


def _citable(match, citations, field_name: str) -> int:
    support = match.supports.get(field_name)
    if support is None:
        return 0
    return len(citations.renderable_claims(support.satisfied_by))


def _partition(result, citations) -> tuple[list[str], list[str]]:
    """Names the answer should be able to prove, and names it should not.

    Computed from the citation set rather than hardcoded, because *which* match starves is
    not fixed: chunks are cited before claims and both are consumed in retrieval order, so
    flooding one source with chunks can starve either source's claims depending on how the
    two rank. Hardcoding a name made the test assert an ordering it had no business
    depending on. What the fix actually promises is that the rendered set equals the
    provable set, and that is what these tests check.
    """
    structured = result.structured
    assert structured is not None
    required = structured.plan.required_claim_fields
    provable: list[str] = []
    starved: list[str] = []
    for match in structured.matches:
        ok = all(_citable(match, citations, field) > 0 for field in required)
        (provable if ok else starved).append(match.entity.canonical_name)
    return provable, starved


class TestTheLeakIsReproduced:
    """Precondition: one match really does lose provenance for a required constraint.

    Without this, every assertion below could pass because both matches were fully
    citable and nothing was ever withheld.
    """

    def test_one_match_has_an_uncitable_required_constraint(
        self, session, corpus
    ) -> None:
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        result, citations = _parsed(session)
        structured = result.structured
        assert structured is not None
        assert len(structured.matches) == 2, "both must qualify in the executor"

        starved = [
            match
            for match in structured.matches
            if any(
                _citable(match, citations, field_name) == 0
                for field_name in structured.plan.required_claim_fields
            )
        ]
        assert len(starved) == 1, (
            "exactly one match must be missing provenance for a required constraint"
        )

    def test_required_claim_fields_covers_the_parsed_constraints(
        self, session, corpus
    ) -> None:
        _restaurant(corpus, "小林日料")
        result, _ = _parsed(session)
        assert result.structured is not None
        assert result.structured.plan.required_claim_fields == frozenset(
            {"cuisine", "price_per_person"}
        )


class TestUnprovableMatchIsNotPresented:
    """The leak itself: no match may be listed as 符合条件 without citable support."""

    def test_the_starved_match_is_not_listed(self, session, corpus) -> None:
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        result, citations, answer = _answer(session, _plan_from(session))
        assert answer.generator == "structured"
        provable, starved = _partition(result, citations)
        assert len(starved) == 1, "precondition: exactly one match must be unprovable"
        listed = _listed(answer.content)
        assert provable == [name for name in listed], (
            "the provable match must still be reported"
        )
        assert starved[0] not in listed, (
            "a match whose required constraint has no citation was presented as qualifying"
        )

    def test_the_count_matches_what_is_listed(self, session, corpus) -> None:
        """N in 找到 N 个符合条件的结果 is itself a claim about the collection."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        _, _, answer = _answer(session, _plan_from(session))
        assert _count(answer.content) == len(_listed(answer.content)) == 1

    def test_structured_result_still_contains_the_withheld_match(
        self, session, corpus
    ) -> None:
        """Diagnostics stay truthful about what the executor found."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        result, citations, answer = _answer(session, _plan_from(session))
        assert result.structured is not None
        _, starved = _partition(result, citations)
        names = [m.entity.canonical_name for m in result.structured.matches]
        assert starved[0] in names, "the executor's finding must not be rewritten"
        assert len(result.structured.matches) == 2

    def test_the_withholding_is_disclosed(self, session, corpus) -> None:
        """Silently dropping it would misreport the search as having found less."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        _, _, answer = _answer(session, _plan_from(session))
        assert "拿不到可核查的出处" in answer.content

    def test_the_disclosure_is_a_system_statement(self, session, corpus) -> None:
        """It describes this answer's limits, so it must not carry creator provenance."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        _, _, answer = _answer(session, _plan_from(session))
        line = next(
            line
            for line in answer.content.splitlines()
            if "拿不到可核查的出处" in line
        )
        assert not re.findall(r"\[\d+\]", line)

    def test_no_uncited_constraint_line_survives(self, session, corpus) -> None:
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        _, _, answer = _answer(session, _plan_from(session))
        for line in answer.content.splitlines():
            if line.strip().startswith("- ") and "：" in line and "来自" in line:
                assert re.findall(r"\[\d+\]", line), (
                    f"constraint stated with no provenance: {line!r}"
                )

    def test_every_listed_match_has_a_cited_line_per_required_field(
        self, session, corpus
    ) -> None:
        """The positive form of the invariant, read off the rendered text.

        A match is listed only if the answer shows, with a marker, a value for every
        claim-derived required constraint. This is what the user needs in order to check
        the qualification rather than take it on faith.
        """
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        result, _, answer = _answer(session, _plan_from(session))
        assert result.structured is not None
        labels = {"cuisine": "菜系", "price_per_person": "人均", "district": "地区"}
        blocks = re.split(r"^\d+\. ", answer.content, flags=re.MULTILINE)[1:]
        assert blocks, "precondition: something was listed"
        for block in blocks:
            for field_name in result.structured.plan.required_claim_fields:
                label = labels[field_name]
                cited = [
                    line
                    for line in block.splitlines()
                    if line.strip().startswith(f"- {label}")
                    and re.findall(r"\[\d+\]", line)
                ]
                assert cited, f"listed match shows no cited {label}: {block!r}"


def _plan_from(session: Session) -> QueryPlan:
    """The parsed plan for `QUERY`, so tests exercise the real parser output."""
    from douyin_knowledge.retrieval.query_parser import parse_query

    plan, _ = parse_query(QUERY, scope="personal_required", limit=10)
    return plan


class TestFullyCitableMatchIsUnaffected:
    """The feature must survive the fix."""

    def test_a_single_provable_match_renders_normally(self, session, corpus) -> None:
        _restaurant(corpus, "大山日料", price=90)
        _, _, answer = _answer(session, _plan_from(session))
        assert _listed(answer.content) == ["大山日料"]
        assert _count(answer.content) == 1
        assert "人均" in answer.content
        assert "菜系" in answer.content
        assert answer.has_evidence is True
        assert "拿不到可核查的出处" not in answer.content

    def test_two_provable_matches_both_render(self, session, corpus) -> None:
        _restaurant(corpus, "大山日料", price=90)
        _restaurant(corpus, "小林日料", price=80)
        _, _, answer = _answer(session, _plan_from(session))
        assert set(_listed(answer.content)) == {"大山日料", "小林日料"}
        assert _count(answer.content) == 2


class TestEveryRequiredConstraintMustBeCitable:
    """Not "at least one citation somewhere": each required condition on its own."""

    def test_a_cited_cuisine_does_not_prove_an_uncited_price(
        self, session, corpus
    ) -> None:
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD_PARTIAL)
        second = _restaurant(corpus, "大山日料", price=90)
        # The cuisine sentence is retrievable on its own; the price sentence is not. So this
        # match's cuisine can be cited through the chunk and its price cannot be cited at
        # all -- which is the precondition, not an accident of the budget.
        _chunk_for_claim(corpus, second, "cuisine", "大山日料 是日料")
        result, citations, answer = _answer(session, _plan_from(session))
        assert result.structured is not None
        _, starved_names = _partition(result, citations)
        assert len(starved_names) == 1
        starved = next(
            m
            for m in result.structured.matches
            if m.entity.canonical_name == starved_names[0]
        )
        # One required field IS citable for this match, so an "any citation for the entity"
        # rule would have let it render.
        assert _citable(starved, citations, "cuisine") > 0
        assert _citable(starved, citations, "price_per_person") == 0
        assert starved_names[0] not in _listed(answer.content)

    def test_an_excerpt_from_the_same_source_does_not_prove_the_constraint(
        self, session, corpus
    ) -> None:
        """The withheld match's own source IS cited -- just not for the constraint."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD_PARTIAL)
        second = _restaurant(corpus, "大山日料", price=90)
        _chunk_for_claim(corpus, second, "cuisine", "大山日料 是日料")
        result, citations, answer = _answer(session, _plan_from(session))
        assert result.structured is not None
        _, starved_names = _partition(result, citations)
        assert len(starved_names) == 1
        starved = next(
            m
            for m in result.structured.matches
            if m.entity.canonical_name == starved_names[0]
        )
        cited_sources = {c.source_id for c in citations.citations}
        assert set(starved.source_ids) & cited_sources, (
            "precondition: the withheld match's source is cited somewhere, so a rule of "
            "'any citation for this entity' would have accepted it"
        )
        assert starved_names[0] not in _listed(answer.content)

    def test_a_district_constraint_is_claim_derived_too(self, session, corpus) -> None:
        """`plan.location` executes as a required `located_in` claim constraint.

        It is not in `plan.claim_constraints`, so a fix keyed only off that tuple would
        treat district qualification as needing no provenance.
        """
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        plan = QueryPlan(
            raw_query=QUERY,
            scope="personal_required",
            location=LocationConstraint(district="徐汇区"),
            limit=10,
        )
        assert "district" in plan.required_claim_fields
        result, citations, answer = _answer(session, plan)
        assert result.structured is not None and result.structured.matches
        match = result.structured.matches[0]
        if _citable(match, citations, "district") == 0:
            assert "小林日料" not in _listed(answer.content)
        else:
            assert "小林日料" in _listed(answer.content)

    def test_effective_constraints_match_what_the_executor_enforced(self) -> None:
        """The synthesis moved onto the plan; this pins that it still happens."""
        plan = QueryPlan(
            raw_query=QUERY,
            location=LocationConstraint(district="徐汇区"),
            claim_constraints=(
                ClaimConstraint(field="cuisine", operator="=", value_text="日料"),
            ),
        )
        fields = [c.field for c in plan.effective_claim_constraints]
        assert fields == ["cuisine", "district"]
        assert plan.required_claim_fields == frozenset({"cuisine", "district"})


class TestOptionalConstraintsStayOptional:
    """An optional constraint must not become mandatory because provenance is enforced."""

    def test_an_uncitable_optional_constraint_does_not_withhold_the_match(
        self, session, corpus
    ) -> None:
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD_PARTIAL)
        second = _restaurant(corpus, "大山日料", price=90)
        _chunk_for_claim(corpus, second, "cuisine", "大山日料 是日料")
        plan = QueryPlan(
            raw_query=QUERY,
            scope="personal_required",
            claim_constraints=(
                ClaimConstraint(field="cuisine", operator="=", value_text="日料"),
                ClaimConstraint(
                    field="price_per_person",
                    operator="<",
                    value_number=100,
                    required=False,
                ),
            ),
            limit=10,
        )
        assert plan.required_claim_fields == frozenset({"cuisine"})
        result, citations, answer = _answer(session, plan)
        assert result.structured is not None
        # Precondition: some match's price genuinely has no citation. Were the rule to leak
        # into optional constraints, that match would be withheld -- so the same corpus that
        # proves withholding works must here prove it does *not* fire.
        starved = [
            match
            for match in result.structured.matches
            if _citable(match, citations, "price_per_person") == 0
            and _citable(match, citations, "cuisine") > 0
        ]
        assert starved, "precondition: an uncitable optional constraint must exist"
        listed = _listed(answer.content)
        for match in starved:
            assert match.entity.canonical_name in listed, (
                "an optional constraint became mandatory: the match was withheld for "
                "missing provenance it was never required to have"
            )
        assert _count(answer.content) == len(result.structured.matches)

    def test_an_optional_constraint_is_not_stated_without_provenance(
        self, session, corpus
    ) -> None:
        """It may go unrendered; it may not be asserted bare."""
        _restaurant(corpus, "小林日料", price=80, flood=FLOOD)
        _restaurant(corpus, "大山日料", price=90)
        plan = QueryPlan(
            raw_query=QUERY,
            scope="personal_required",
            claim_constraints=(
                ClaimConstraint(field="cuisine", operator="=", value_text="日料"),
                ClaimConstraint(
                    field="price_per_person",
                    operator="<",
                    value_number=100,
                    required=False,
                ),
            ),
            limit=10,
        )
        _, _, answer = _answer(session, plan)
        for line in answer.content.splitlines():
            if line.strip().startswith("- 人均"):
                assert re.findall(r"\[\d+\]", line)


class TestUserStateOnlyBehaviorPreserved:
    """DEC-018: the user's own declaration needs no creator claim to be reportable."""

    def test_a_user_state_only_match_still_renders(self, session, corpus) -> None:
        entity = corpus.entity("想去的店")
        corpus.user_state(entity, "want_to_go")
        plan = QueryPlan(
            raw_query="我想去的店",
            scope="personal_required",
            user_state=UserStateConstraint(state="want_to_go"),
            limit=10,
        )
        assert plan.required_claim_fields == frozenset()
        result, _, answer = _answer(session, plan, query="我想去的店")
        assert result.structured is not None
        assert [m.entity.canonical_name for m in result.structured.matches] == ["想去的店"]
        assert _listed(answer.content) == ["想去的店"]
        assert _count(answer.content) == 1
        assert "拿不到可核查的出处" not in answer.content

    def test_user_state_only_match_has_no_claim_lines_to_cite(
        self, session, corpus
    ) -> None:
        """It cites nothing, and that is correct rather than a grounding failure."""
        entity = corpus.entity("想去的店")
        corpus.user_state(entity, "want_to_go")
        plan = QueryPlan(
            raw_query="我想去的店",
            scope="personal_required",
            user_state=UserStateConstraint(state="want_to_go"),
            limit=10,
        )
        _, citations, answer = _answer(session, plan, query="我想去的店")
        assert len(citations) == 0
        assert answer.has_evidence is False
        for line in answer.content.splitlines():
            assert not line.strip().startswith("- ")

    def test_subtype_narrowing_needs_no_creator_evidence(self, session, corpus) -> None:
        """Subtype is our annotation, so it must not join the required-provenance set."""
        entity = corpus.entity("想去的店")
        entity.subtype = "restaurant"
        corpus.session.flush()
        corpus.user_state(entity, "want_to_go")
        plan = QueryPlan(
            raw_query="我想去的日料店",
            scope="personal_required",
            entity_types=("place",),
            entity_subtypes=("restaurant",),
            user_state=UserStateConstraint(state="want_to_go"),
            limit=10,
        )
        assert plan.required_claim_fields == frozenset()
        _, _, answer = _answer(session, plan, query="我想去的日料店")
        assert _listed(answer.content) == ["想去的店"]
