"""Test model definitions and database schema.

Verifies:
- All models can be imported
- Tables are created correctly
- Basic CRUD operations work
- Relationships are defined correctly
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from douyin_knowledge.db.base import Base
from douyin_knowledge.models import (
    Claim,
    Conversation,
    Entity,
    EntityMention,
    EvidenceUnit,
    Message,
    ProcessingRun,
    Source,
    Topic,
    WikiPage,
    WikiRevision,
)


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


def test_all_models_import():
    """Verify all models can be imported."""
    assert Source is not None
    assert EvidenceUnit is not None
    assert ProcessingRun is not None
    assert EntityMention is not None
    assert Entity is not None
    assert Claim is not None
    assert Topic is not None
    assert WikiPage is not None
    assert WikiRevision is not None
    assert Conversation is not None
    assert Message is not None
    assert Message is not None


def test_tables_created(engine):
    """Verify all expected tables are created."""
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    expected_tables = {
        "sources",
        "creators",
        "collections",
        "source_assets",
        "evidence_units",
        "processing_runs",
        "processing_run_evidence",
        "retrieval_chunks",
        "retrieval_chunk_evidence",
        "knowledge_items",
        "knowledge_item_labels",
        "knowledge_item_topics",
        "entity_mentions",
        "entities",
        "claims",
        "claim_evidence",
        "topics",
        "wiki_pages",
        "wiki_revisions",
        "wiki_supports",
        "wiki_links",
        "wiki_integration_runs",
        "wiki_lint_findings",
        "quality_rules",
        "quality_rule_samples",
        "quality_ledger",
        "user_annotations",
        "conversations",
        "messages",
        "message_citations",
        "conversation_state",
    }

    for table in expected_tables:
        assert table in tables, f"Table {table} not created"


def test_source_crud(session):
    """Test basic CRUD on Source model."""
    # Create
    source = Source(
        platform="douyin",
        external_id="test123",
        source_type="video",
        caption_raw="测试视频",
        source_url="https://example.com/video/test123",
    )
    session.add(source)
    session.commit()

    assert source.id is not None

    # Read
    retrieved = session.query(Source).filter(Source.external_id == "test123").first()
    assert retrieved is not None
    assert retrieved.caption_raw == "测试视频"

    # Update
    retrieved.caption_raw = "更新的标题"
    session.commit()

    updated = session.query(Source).filter(Source.id == source.id).first()
    assert updated.caption_raw == "更新的标题"

    # Delete
    session.delete(updated)
    session.commit()

    deleted = session.query(Source).filter(Source.id == source.id).first()
    assert deleted is None


def test_evidence_relationship(session):
    """Test Source → EvidenceUnit relationship."""
    # Create source
    source = Source(
        platform="douyin",
        external_id="test456",
        source_type="video",
        source_url="https://example.com/test456",
    )
    session.add(source)
    session.flush()

    # Create processing run
    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        processor_version="0.1.0",
        schema_version="1.0",
        target_level=2,
        status="succeeded",
    )
    session.add(run)
    session.flush()

    # Create evidence units
    evidence1 = EvidenceUnit(
        source_id=source.id,
        kind="caption",
        normalized_text="视频标题",
        content_hash="hash1",
    )
    evidence2 = EvidenceUnit(
        source_id=source.id,
        kind="transcript",
        normalized_text="转录文本",
        start_ms=0,
        end_ms=5000,
        content_hash="hash2",
    )
    session.add_all([evidence1, evidence2])
    session.commit()

    # Verify relationship
    retrieved_source = session.query(Source).filter(Source.id == source.id).first()
    evidence_units = (
        session.query(EvidenceUnit)
        .filter(EvidenceUnit.source_id == retrieved_source.id)
        .all()
    )
    assert len(evidence_units) == 2


def test_entity_mention_resolution(session):
    """Test EntityMention → Entity resolution."""
    # Create source and evidence
    source = Source(
        platform="douyin",
        external_id="test789",
        source_type="video",
        source_url="https://example.com/test789",
    )
    session.add(source)
    session.flush()

    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        processor_version="0.1.0",
        schema_version="1.0",
        target_level=2,
        status="succeeded",
    )
    session.add(run)
    session.flush()

    evidence = EvidenceUnit(
        source_id=source.id,
        kind="caption",
        normalized_text="好运茶餐厅很不错",
        content_hash="hash3",
    )
    session.add(evidence)
    session.flush()

    # Create canonical entity
    entity = Entity(
        entity_type="place",
        canonical_name="好运茶餐厅",
        normalized_name="好运茶餐厅",
    )
    session.add(entity)
    session.flush()

    # Create mention and link to entity
    mention = EntityMention(
        source_id=source.id,
        processing_run_id=run.id,
        mention_text="好运茶餐厅",
        normalized_text="好运茶餐厅",
        entity_type_hint="place",
        resolved_entity_id=entity.id,
    )
    session.add(mention)
    session.commit()

    # Verify resolution
    retrieved_mention = session.query(EntityMention).filter(EntityMention.id == mention.id).first()
    assert retrieved_mention.resolved_entity_id == entity.id

    retrieved_entity = session.query(Entity).filter(Entity.id == entity.id).first()
    assert retrieved_entity.canonical_name == "好运茶餐厅"


def test_claim_extraction(session):
    """Test Claim extraction with entity linkage."""
    # Setup
    source = Source(
        platform="douyin",
        external_id="test_claim",
        source_type="video",
        source_url="https://example.com/test_claim",
    )
    session.add(source)
    session.flush()

    run = ProcessingRun(
        source_id=source.id,
        run_kind="full",
        processor_version="0.1.0",
        schema_version="1.0",
        target_level=2,
        status="succeeded",
    )
    session.add(run)
    session.flush()

    evidence = EvidenceUnit(
        source_id=source.id,
        kind="transcript",
        normalized_text="这家店人均80块钱",
        start_ms=1000,
        end_ms=3000,
        content_hash="hash4",
    )
    session.add(evidence)
    session.flush()

    entity = Entity(
        entity_type="place",
        canonical_name="测试餐厅",
        normalized_name="测试餐厅",
    )
    session.add(entity)
    session.flush()

    # Create claim
    claim = Claim(
        source_id=source.id,
        processing_run_id=run.id,
        subject_entity_id=entity.id,
        predicate="price_per_person",
        value_type="number",
        value_number=80,
        currency="CNY",
        claim_kind="factual",
        provenance_type="direct",
        attribution="creator",
        confidence=0.9,
    )
    session.add(claim)
    session.commit()

    # Verify
    retrieved_claim = session.query(Claim).filter(Claim.id == claim.id).first()
    assert retrieved_claim.predicate == "price_per_person"
    assert retrieved_claim.subject_entity_id == entity.id
    assert retrieved_claim.value_number == 80


def test_wiki_entry_with_fields(session):
    """Test WikiPage with WikiRevisions and citations."""
    # Setup entity
    entity = Entity(
        entity_type="place",
        canonical_name="测试地点",
        normalized_name="测试地点",
    )
    session.add(entity)
    session.flush()

    # Create wiki entry
    wiki = WikiPage(
        page_type="entity",
        canonical_key="entity:place:测试地点",
        title="测试地点",
        slug="测试地点",
        entity_id=entity.id,
    )
    session.add(wiki)
    session.flush()

    # Create wiki revision
    revision = WikiRevision(
        page_id=wiki.id,
        revision_no=1,
        content_markdown="# 测试地点\n\n这是一个测试地点",
        summary="Initial revision",
        content_hash="abc123",
    )
    session.add(revision)
    session.commit()

    # Verify
    retrieved_wiki = session.query(WikiPage).filter(WikiPage.id == wiki.id).first()
    assert retrieved_wiki.title == "测试地点"

    revisions = session.query(WikiRevision).filter(WikiRevision.page_id == wiki.id).all()
    assert len(revisions) == 1
    assert revisions[0].revision_no == 1


def test_conversation_flow(session):
    """Test Conversation → Message → Message flow."""
    # Create conversation
    conv = Conversation(
        title="测试对话",
    )
    session.add(conv)
    session.flush()

    # Create user message
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content="附近有什么好吃的？",
    )
    session.add(user_msg)
    session.flush()

    # Create assistant message
    assistant_msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content="根据您的收藏，推荐好运茶餐厅...",
    )
    session.add(assistant_msg)
    session.commit()

    # Verify
    retrieved_conv = session.query(Conversation).filter(Conversation.id == conv.id).first()
    assert retrieved_conv is not None
    messages = session.query(Message).filter(Message.conversation_id == conv.id).all()
    assert len(messages) == 2

    user_messages = [m for m in messages if m.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "附近有什么好吃的？"
