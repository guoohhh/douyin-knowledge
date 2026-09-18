"""Test knowledge extraction pipeline.

Verifies:
- Entity mention extraction
- Entity resolution
- Claim extraction
- Knowledge aggregation
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from douyin_knowledge.ai.adapters.mock_adapter import MockStructuredModel
from douyin_knowledge.db.base import Base
from douyin_knowledge.db.models.entities import (
    Claim,
    ClaimEvidence,
    Entity,
    EntityMention,
    EntityMentionEvidence,
)
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun
from douyin_knowledge.extraction.claim_extractor import ClaimExtractor
from douyin_knowledge.extraction.entity_extractor import EntityExtractor
from douyin_knowledge.extraction.entity_resolver import EntityResolver
from douyin_knowledge.models import Source


@pytest.fixture
def engine():
    """In-memory SQLite engine for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    """Database session for testing."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def sample_source_with_evidence(session):
    """Create a sample source with evidence for testing."""
    source = Source(
        platform="douyin",
        external_id="test_extract",
        source_type="video",
        source_url="https://example.com/test_extract",
        caption_raw="今天去了好运茶餐厅，人均80块，波萝油很好吃！",
    )
    session.add(source)
    session.flush()

    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        schema_version="1.0",
        target_level=2,
        processor_version="0.1.0",
        status="succeeded",
    )
    session.add(run)
    session.flush()

    evidence = EvidenceUnit(
        source_id=source.id,

        kind="caption",
        content_hash="hash_cap",
        normalized_text="今天去了好运茶餐厅，人均80块，波萝油很好吃！",
    )
    session.add(evidence)
    session.commit()

    return source, run, evidence


def test_entity_extractor_basic(session, sample_source_with_evidence):
    """The extractor must find mentions in real text, not just persist rows.

    Constructing an EntityMention by hand only proves SQLAlchemy works. This
    calls extract_from_evidence so a schema or prompt regression actually fails.
    """
    source, run, evidence = sample_source_with_evidence

    extractor = EntityExtractor(MockStructuredModel())
    mentions = extractor.extract_from_evidence(
        session,
        [evidence],
        source_id=source.id,
        processing_run_id=run.id,
    )
    session.commit()

    assert mentions, "caption text names a restaurant; extraction must find it"
    place_mentions = [m for m in mentions if m.entity_type_hint == "place"]
    assert place_mentions
    assert any("好运茶餐厅" in m.mention_text for m in place_mentions)

    # Every mention must be linked to the evidence it came from, or it is not
    # citable and cannot ground a claim.
    for mention in mentions:
        links = session.scalars(
            select(EntityMentionEvidence).where(
                EntityMentionEvidence.entity_mention_id == mention.id
            )
        ).all()
        assert links, f"mention {mention.mention_text} has no evidence link"


def test_entity_extractor_rejects_text_not_present(session, sample_source_with_evidence):
    """Hallucination guard: a mention absent from the text must be dropped."""
    source, run, evidence = sample_source_with_evidence

    class HallucinatingModel:
        def extract(self, prompt, schema, **kwargs):
            from douyin_knowledge.ai.providers import StructuredResponse

            return StructuredResponse(
                data={
                    "mentions": [
                        {"text": "米其林三星龙虾馆", "entity_type": "place", "confidence": 0.99}
                    ]
                },
                model="hallucinating",
            )

    extractor = EntityExtractor(HallucinatingModel())
    mentions = extractor.extract_from_evidence(
        session,
        [evidence],
        source_id=source.id,
        processing_run_id=run.id,
    )
    session.commit()

    assert mentions == [], "a mention not literally present in the evidence must be rejected"


def test_entity_resolver_deduplication(session):
    """Test that entity resolver merges duplicate mentions."""
    # Create source
    source = Source(
        platform="douyin",
        external_id="test_resolve",
        source_type="video",
        source_url="https://example.com/test_resolve",
    )
    session.add(source)
    session.flush()

    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        schema_version="1.0",
        target_level=2,
        processor_version="0.1.0",
        status="succeeded",
    )
    session.add(run)
    session.flush()

    evidence = EvidenceUnit(
        source_id=source.id,

        kind="caption",
        content_hash="hash_cap",
        normalized_text="test",
    )
    session.add(evidence)
    session.flush()

    # Create multiple mentions of the same entity
    mention1 = EntityMention(
        source_id=source.id,
        processing_run_id=run.id,
        mention_text="好运茶餐厅",
        entity_type_hint="place",
        normalized_text="好运茶餐厅",
    )
    mention2 = EntityMention(
        source_id=source.id,
        processing_run_id=run.id,
        mention_text="好运餐厅",
        entity_type_hint="place",
        normalized_text="好运茶餐厅",
    )
    mention3 = EntityMention(
        source_id=source.id,
        processing_run_id=run.id,
        mention_text="好运",
        entity_type_hint="place",
        normalized_text="好运茶餐厅",
    )
    session.add_all([mention1, mention2, mention3])
    session.commit()

    # Resolve entities using resolver
    resolver = EntityResolver()

    # Resolve each mention
    entity1 = resolver.resolve_mention(session, mention1)
    entity2 = resolver.resolve_mention(session, mention2)
    entity3 = resolver.resolve_mention(session, mention3)
    session.commit()

    # The three mentions share a normalized form, which is the only signal trusted for
    # automatic merge (ENT-002), so they must collapse to one entity. This is the actual
    # subject of the test; it previously discarded these three results and asserted only
    # that "resolution was attempted", which any non-crashing resolver would satisfy.
    assert entity1.entity_id is not None
    assert entity1.entity_id == entity2.entity_id == entity3.entity_id
    assert not any(o.needs_review for o in (entity1, entity2, entity3)), (
        "an exact normalized match is the one signal trusted for automatic merge, so "
        "these must not land in the review queue"
    )

    # Verify resolution happened
    session.refresh(mention1)
    session.refresh(mention2)
    session.refresh(mention3)

    assert mention1.resolved_entity_id is not None
    # At minimum, verify that resolution was attempted
    resolved_entities = session.query(Entity).all()
    assert len(resolved_entities) > 0


def test_claim_extractor_price(session, sample_source_with_evidence):
    """人均80块 must become a *numeric* claim, not a string.

    A price stored as text cannot be filtered or compared, which breaks the
    product's core query shape ("人均 < 100").
    """
    source, run, evidence = sample_source_with_evidence

    mentions = EntityExtractor(MockStructuredModel()).extract_from_evidence(
        session, [evidence], source_id=source.id, processing_run_id=run.id
    )
    claims = ClaimExtractor(MockStructuredModel()).extract_from_evidence(
        session,
        [evidence],
        source_id=source.id,
        processing_run_id=run.id,
        mentions=mentions,
        creator_name="测试作者",
    )
    session.commit()

    price_claims = [c for c in claims if c.predicate == "price_per_person"]
    assert price_claims, f"expected a price claim, got {[c.predicate for c in claims]}"

    claim = price_claims[0]
    assert claim.value_number == 80.0
    assert claim.value_type == "number"
    assert claim.currency == "CNY"
    assert claim.attribution == "测试作者"

    # Provenance must be intact: a claim with no evidence link is unciteable.
    links = session.scalars(
        select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)
    ).all()
    assert links


def test_claim_extractor_marks_opinion_as_opinion(session, sample_source_with_evidence):
    """KM-003: "很好吃" is the creator's evaluation, never a global fact."""
    source, run, evidence = sample_source_with_evidence

    claims = ClaimExtractor(MockStructuredModel()).extract_from_evidence(
        session,
        [evidence],
        source_id=source.id,
        processing_run_id=run.id,
        creator_name="测试作者",
    )
    session.commit()

    assert claims
    for claim in claims:
        assert claim.provenance_type in {
            "creator_statement",
            "creator_opinion",
            "on_screen_text",
            "third_party",
        }
        # Nothing may be attributed to nobody; an unattributed claim reads as
        # objective truth.
        assert claim.attribution


def test_entity_attributes(session):
    """Test that entity attributes are stored correctly."""
    entity = Entity(
        entity_type="place",
        normalized_name="好运茶餐厅",
        canonical_name="测试餐厅",
        profile_json={
            "category": "restaurant",
            "cuisine": "粤菜",
            "location": {"city": "香港", "district": "油尖旺"},
        },
    )
    session.add(entity)
    session.commit()

    retrieved = session.query(Entity).filter(
        Entity.canonical_name == "测试餐厅"
    ).first()

    assert retrieved.profile_json["category"] == "restaurant"
    assert retrieved.profile_json["cuisine"] == "粤菜"
    assert retrieved.profile_json["location"]["city"] == "香港"


def test_claim_confidence_tracking(session, sample_source_with_evidence):
    """Test that claim extraction includes confidence scores."""
    source, run, evidence = sample_source_with_evidence

    claim = Claim(
        source_id=source.id,
        processing_run_id=run.id,
        predicate="price",
        subject_text="餐厅",
        value_type="text",
        value_text="人均80块",
        claim_kind="factual",
        provenance_type="direct",
        attribution="creator",
        confidence=0.85,
    )
    session.add(claim)
    session.commit()

    retrieved = session.query(Claim).filter(Claim.id == claim.id).first()
    assert retrieved.confidence == 0.85


def test_structured_value_storage(session, sample_source_with_evidence):
    """Test that structured values are stored and retrieved correctly."""
    source, run, evidence = sample_source_with_evidence

    claim = Claim(
        source_id=source.id,
        processing_run_id=run.id,
        predicate="hours",
        subject_text="餐厅",
        value_type="structured",
        value_text="营业时间9:00-22:00",
        value_json={
            "type": "hours",
            "open": "09:00",
            "close": "22:00",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        },
        claim_kind="factual",
        provenance_type="direct",
        attribution="creator",
    )
    session.add(claim)
    session.commit()

    retrieved = session.query(Claim).filter(Claim.id == claim.id).first()
    assert retrieved.value_json["open"] == "09:00"
    assert retrieved.value_json["close"] == "22:00"
    assert len(retrieved.value_json["days"]) == 7


def test_mention_count_tracking(session):
    """Test that canonical entities track mention counts."""
    source = Source(
        platform="douyin",
        external_id="test_count",
        source_type="video",
        source_url="https://example.com/test_count",
    )
    session.add(source)
    session.flush()

    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        schema_version="1.0",
        target_level=2,
        processor_version="0.1.0",
        status="succeeded",
    )
    session.add(run)
    session.flush()

    evidence = EvidenceUnit(
        source_id=source.id,

        kind="caption",
        content_hash="hash_cap",
        normalized_text="test",
    )
    session.add(evidence)
    session.flush()

    entity = Entity(
        entity_type="place",
        normalized_name="好运茶餐厅",
        canonical_name="测试地点",
    )
    session.add(entity)
    session.flush()

    # Add mentions
    for i in range(3):
        mention = EntityMention(
            source_id=source.id,
            processing_run_id=run.id,
            mention_text=f"测试{i}",
            entity_type_hint="place",
            normalized_text="好运茶餐厅",
            resolved_entity_id=entity.id,
        )
        session.add(mention)

    session.commit()

    # Update mention count
    mention_count = session.query(EntityMention).filter(
        EntityMention.resolved_entity_id == entity.id
    ).count()

    assert mention_count == 3
