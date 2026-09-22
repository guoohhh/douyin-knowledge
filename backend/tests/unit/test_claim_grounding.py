"""Claim grounding validation (P1-2, DEC-017).

The property under test: naming a real evidence id is not the same as being supported by
it. A model can return a well-formed claim whose quoted span does not appear in the
evidence, or whose number contradicts it, and before this validator both were stored and
then rendered on a wiki page with a citation attached.

The expensive direction of error runs both ways, so both are asserted: a hallucinated
number must not survive, and a legitimate extraction must not be thrown away because the
model quoted a sentence with different punctuation.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.ai.providers import StructuredResponse
from douyin_knowledge.core.text import content_hash
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.extraction.claim_extractor import ClaimExtractor
from douyin_knowledge.extraction.grounding import GroundingValidator

TRANSCRIPT = "好运茶餐厅人均八十块，叉烧饭很不错，我推荐大家去试试。"


@pytest.fixture
def source(session: Session) -> Source:
    row = Source(
        platform="douyin",
        external_id="s_grounding",
        source_type="video",
        title="好运茶餐厅",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def other_source(session: Session) -> Source:
    row = Source(
        platform="douyin", external_id="s_other", source_type="video", title="别的店"
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def run(session: Session, source: Source) -> ProcessingRun:
    row = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        schema_version="1.0",
        processor_version="0.1.0",
        status="running",
        target_level=2,
        achieved_level=2,
        started_at_ms=1_000,
    )
    session.add(row)
    session.flush()
    return row


def _evidence(session: Session, source: Source, text: str = TRANSCRIPT) -> EvidenceUnit:
    row = EvidenceUnit(
        source_id=source.id,
        kind="transcript",
        raw_text=text,
        normalized_text=text,
        content_hash=content_hash("ev", text),
    )
    session.add(row)
    session.flush()
    return row


class ScriptedModel:
    """Returns claim payloads handed to it, so a test can script a hallucination."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)

    def extract(
        self, prompt: str, schema: dict[str, Any], *, temperature: float = 0.0
    ) -> StructuredResponse:
        return StructuredResponse(
            data={"claims": self.payloads}, model="scripted", usage=None, raw_text="{}"
        )


def _claim(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject_text": "好运茶餐厅",
        "predicate": "price_per_person",
        "value_type": "number",
        "value_number": 80,
        "currency": "CNY",
        "claim_kind": "measurement",
        "provenance_type": "creator_statement",
        "confidence": 0.9,
        "evidence_span": "好运茶餐厅人均八十块",
    }
    payload.update(over)
    return payload


# ------------------------------------------------------------------ validator


class TestGroundingValidator:
    def test_a_grounded_claim_passes_and_records_where_it_matched(
        self, session: Session, source: Source
    ) -> None:
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="人均八十块",
            value_type="number",
            value_number=80,
        )

        assert verdict.passed
        assert verdict.status == "valid"
        assert verdict.span_offset is not None

    def test_evidence_from_another_source_is_a_hard_rejection(
        self, session: Session, source: Source, other_source: Source
    ) -> None:
        """Structural, not stylistic: the provenance spine would be a lie.

        `Claim -> ClaimEvidence -> EvidenceUnit -> Source` is what makes a citation mean
        anything, and a claim written against one source citing another's evidence breaks
        it in a way no amount of confidence adjustment repairs.
        """
        verdict = GroundingValidator().validate(
            _evidence(session, other_source),
            expected_source_id=source.id,
            evidence_span="人均八十块",
        )

        assert not verdict.passed
        assert verdict.status == "rejected_context"
        assert other_source.id in (verdict.reason or "")

    def test_punctuation_and_width_differences_are_not_hallucinations(
        self, session: Session, source: Source
    ) -> None:
        """The model quoting with different punctuation is the common case, not a defect.

        Rejecting these would be the expensive kind of wrong: correct extractions thrown
        away, silently, at a rate nobody notices.
        """
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            # full-width comma added, trailing punctuation, different spacing
            evidence_span="好运茶餐厅人均八十块，",
            value_type="number",
            value_number=80,
        )

        assert verdict.passed, verdict.reason

    def test_a_number_the_evidence_contradicts_is_rejected(
        self, session: Session, source: Source
    ) -> None:
        """The failure mode that matters: a wrong number wearing a real citation."""
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="好运茶餐厅人均八十块",
            value_type="number",
            value_number=800,
        )

        assert not verdict.passed
        assert verdict.status == "rejected_value"

    def test_chinese_numerals_ground_an_arabic_value(
        self, session: Session, source: Source
    ) -> None:
        """A transcript says 八十; the claim stores 80. Those agree.

        Requiring the digits to appear literally would reject every price in a corpus that
        is spoken rather than written -- which is most of this one.
        """
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="人均八十块",
            value_type="number",
            value_number=80,
        )

        assert verdict.passed, verdict.reason

    def test_a_derived_boolean_is_not_required_to_appear_literally(
        self, session: Session, source: Source
    ) -> None:
        """`recommended=true` is a classification, not a quotation.

        The first version of this validator demanded the literal string and silently
        dropped every `recommended` claim in the demo corpus, taking a wiki page with it.
        """
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="我推荐大家去试试",
            value_type="boolean",
            value_text="true",
        )

        assert verdict.passed, verdict.reason

    def test_a_hallucinated_span_downgrades_rather_than_drops(
        self, session: Session, source: Source
    ) -> None:
        """The assertion may be a fair reading even when the quotation is invented.

        So the row survives as an audit trail, but `is_assertable` keeps it out of derived
        output -- see the wiki and retriever tests below.
        """
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="老板说周二休息",  # nowhere in the transcript
            value_type="text",
        )

        assert not verdict.passed
        assert verdict.status == "downgraded"
        assert verdict.confidence_cap == 0.5

    def test_an_unsupported_value_outranks_a_bad_span(
        self, session: Session, source: Source
    ) -> None:
        """Both defects at once must resolve to the rejection, not the downgrade.

        Checking the span first meant the milder verdict won and the worse defect -- an
        invented number -- escaped as merely "downgraded".
        """
        verdict = GroundingValidator().validate(
            _evidence(session, source),
            expected_source_id=source.id,
            evidence_span="老板说周二休息",
            value_type="number",
            value_number=800,
        )

        assert verdict.status == "rejected_value"

    def test_empty_evidence_cannot_ground_anything(
        self, session: Session, source: Source
    ) -> None:
        verdict = GroundingValidator().validate(
            _evidence(session, source, text="   "),
            expected_source_id=source.id,
            evidence_span="人均八十块",
        )

        assert not verdict.passed


# ------------------------------------------------------------------ extractor


class TestExtractorEnforcesGrounding:
    def test_an_ungrounded_number_never_becomes_durable(
        self, session: Session, source: Source, run: ProcessingRun
    ) -> None:
        unit = _evidence(session, source)
        extractor = ClaimExtractor(ScriptedModel(_claim(value_number=800)))  # type: ignore[arg-type]

        claims = extractor.extract_from_evidence(
            session, [unit], source_id=source.id, processing_run_id=run.id
        )

        assert claims == []

    def test_a_grounded_claim_is_stored_with_its_verdict(
        self, session: Session, source: Source, run: ProcessingRun
    ) -> None:
        unit = _evidence(session, source)
        extractor = ClaimExtractor(ScriptedModel(_claim()))  # type: ignore[arg-type]

        claims = extractor.extract_from_evidence(
            session, [unit], source_id=source.id, processing_run_id=run.id
        )

        assert len(claims) == 1
        assert claims[0].grounding_status == "valid"
        assert claims[0].grounding_json["span_offset"] is not None

    def test_a_downgraded_claim_loses_the_span_it_invented(
        self, session: Session, source: Source, run: ProcessingRun
    ) -> None:
        """Stripping the span is the point: it is what would have been rendered as a quote."""
        unit = _evidence(session, source)
        extractor = ClaimExtractor(
            ScriptedModel(  # type: ignore[arg-type]
                _claim(
                    value_type="text",
                    value_number=None,
                    value_text=None,
                    evidence_span="老板说周二休息",
                    confidence=0.95,
                )
            )
        )

        claims = extractor.extract_from_evidence(
            session, [unit], source_id=source.id, processing_run_id=run.id
        )

        assert len(claims) == 1
        claim = claims[0]
        assert claim.grounding_status == "downgraded"
        assert claim.confidence == 0.5, "a confident hallucination is still a hallucination"
        assert not (claim.value_json or {}).get("evidence_span")


# ------------------------------------------------------------------ derived output


class TestDowngradedClaimsStayOutOfDerivedOutput:
    """The half of P1-2 that the user can actually see.

    Capping confidence was the first attempt and it did nothing: no consumer reads the
    field, so a downgraded claim rendered on a wiki page identically to a verified one.
    These tests pin the behaviour to `is_assertable` so a future reader cannot restore the
    cap-only version and believe it works.
    """

    def test_a_downgraded_claim_is_not_composed_into_a_wiki_page(
        self, session: Session, source: Source, run: ProcessingRun
    ) -> None:
        from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity
        from douyin_knowledge.db.models.policy import SourceProcessingState
        from douyin_knowledge.wiki.builder import WikiBuilder

        entity = Entity(
            entity_type="place", canonical_name="好运茶餐厅", normalized_name="好运茶餐厅"
        )
        session.add(entity)
        session.add(
            SourceProcessingState(
                source_id=source.id,
                current_policy_action="process",
                processing_status="succeeded",
                current_processing_run_id=run.id,
            )
        )
        session.flush()

        def _add(status: str | None, predicate: str) -> None:
            # Eligibility rule 6 requires evidence from the claim's own source, so each
            # claim gets a real unit here. This test is about grounding *status*, and a
            # claim with no evidence at all would be excluded for the wrong reason.
            evidence = EvidenceUnit(
                source_id=source.id,
                kind="asr",
                raw_text=f"好运茶餐厅 {predicate}",
                normalized_text=f"好运茶餐厅 {predicate}",
                content_hash=content_hash(f"{predicate}|{status}"),
            )
            session.add(evidence)
            claim = Claim(
                source_id=source.id,
                processing_run_id=run.id,
                subject_entity_id=entity.id,
                subject_text="好运茶餐厅",
                predicate=predicate,
                value_type="number",
                value_number=80,
                currency="CNY",
                claim_kind="measurement",
                provenance_type="creator_statement",
                attribution="unknown_creator",
                grounding_status=status,
            )
            session.add(claim)
            session.flush()
            session.add(ClaimEvidence(claim_id=claim.id, evidence_id=evidence.id))

        _add("valid", "price_per_person")
        _add("downgraded", "closing_hour")
        # Pre-0005 rows carry no verdict and must not vanish on upgrade.
        _add(None, "opening_hour")
        session.flush()

        predicates = {
            c.predicate for c in WikiBuilder(session).claims_for_entity(entity.id)
        }

        assert predicates == {"price_per_person", "opening_hour"}

    def test_is_assertable_grandfathers_unvalidated_claims(self) -> None:
        from douyin_knowledge.extraction.grounding import is_assertable

        assert is_assertable(None)
        assert is_assertable("valid")
        assert not is_assertable("downgraded")
