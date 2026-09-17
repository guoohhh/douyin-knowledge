import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from douyin_knowledge.capture import CapturedSource
from douyin_knowledge.db import Base
from douyin_knowledge.index import rebuild
from douyin_knowledge.models import (
    Claim,
    Entity,
    Evidence,
    Job,
    ProcessingRule,
    ProcessingRun,
    Source,
    UserState,
    WikiPage,
    WikiRevision,
    WikiSupport,
)
from douyin_knowledge.policy import evaluate
from douyin_knowledge.retrieval import answer, search
from douyin_knowledge.service import ingest, process_source, reevaluate, work_once


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")

    @event.listens_for(engine, "connect")
    def pragma(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE VIRTUAL TABLE search_fts USING fts5(source_id UNINDEXED, text)")
        )
    with Session(engine) as db:
        yield db
    engine.dispose()


def demo():
    path = Path(__file__).parents[2] / "fixtures" / "saves.json"
    return [CapturedSource.model_validate(row) for row in json.loads(path.read_text())]


def test_end_to_end_and_provenance(session):
    session.add(ProcessingRule(dimension="collection", value="待看影视", action="metadata_only"))
    session.commit()
    assert ingest(session, demo()) == {"created": 3, "total": 3, "metadata_only": 1}
    assert ingest(session, demo())["created"] == 0
    while work_once(session):
        pass
    assert (
        session.scalar(select(Source).where(Source.external_id == "demo-entertainment-1")).status
        == "metadata_only"
    )
    result = answer(session, "我收藏的旺角日料人均多少？")
    assert result["citations"]
    assert any("80" in cite["text"] for cite in result["citations"])
    for cite in result["citations"]:
        claim = session.get(Claim, cite["claim_id"])
        evidence = session.get(Evidence, claim.evidence_id)
        assert evidence.source_id == claim.source_id
    entity = session.scalar(select(Entity).where(Entity.name == "樱花食堂"))
    page = session.scalar(select(WikiPage).where(WikiPage.key == entity.id))
    revision = session.scalar(
        select(WikiRevision).where(
            WikiRevision.page_id == page.id, WikiRevision.number == page.current_revision
        )
    )
    assert session.scalar(select(WikiSupport).where(WikiSupport.revision_id == revision.id))
    session.add(UserState(entity_id=entity.id, state="visited", rating=4, note="排队久"))
    session.commit()
    assert (
        session.scalar(select(Claim).where(Claim.entity_id == entity.id)).attribution == "creator"
    )
    assert rebuild(session) == 2
    assert search(session, "MCP")


def test_policy_precedence_and_reversal(session):
    ingest(session, demo())
    source = session.scalar(select(Source).where(Source.external_id == "demo-food-1"))
    session.add_all(
        [
            ProcessingRule(dimension="creator", value="food_creator", action="metadata_only"),
            ProcessingRule(dimension="source", value="demo-food-1", action="always_process"),
        ]
    )
    session.commit()
    assert evaluate(session, source)[0] == "always_process"
    source_rule = session.scalar(select(ProcessingRule).where(ProcessingRule.dimension == "source"))
    session.delete(source_rule)
    session.commit()
    reevaluate(session)
    assert source.policy_action == "metadata_only"
    assert source.status == "metadata_only"


def test_reprocessing_keeps_history(session):
    ingest(session, demo()[:1])
    source = session.scalar(select(Source))
    process_source(session, source.id)
    first = source.current_run_id
    process_source(session, source.id)
    assert source.current_run_id != first
    assert session.get(ProcessingRun, first).status == "succeeded"
    assert session.scalar(select(Claim).where(Claim.run_id == first))
    assert len(session.scalars(select(WikiRevision)).all()) >= 2


def test_no_result_and_no_fake_citation(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    result = answer(session, "我收藏的量子香蕉是什么？")
    assert result["citations"] == []
    assert "没有找到" in result["answer"]


def test_failed_run_is_persisted_and_retried(session):
    ingest(session, [CapturedSource(external_id="empty", title="No evidence")])
    source = session.scalar(select(Source).where(Source.external_id == "empty"))
    assert work_once(session)
    session.refresh(source)
    assert source.status == "failed"
    run = session.scalar(select(ProcessingRun).where(ProcessingRun.source_id == source.id))
    assert run.status == "failed"
    assert "No caption" in run.error
    job = session.scalar(select(Job).where(Job.source_id == source.id))
    assert job.status == "queued"
    assert job.attempts == 1


def test_policy_can_hide_previously_processed_source(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    source = session.scalar(select(Source))
    assert source.status == "ready"
    session.add(ProcessingRule(dimension="creator", value="food_creator", action="metadata_only"))
    session.commit()
    reevaluate(session)
    assert source.status == "metadata_only"
    assert search(session, "旺角") == []
    assert source.current_run_id is not None


def test_cheap_content_type_exclusion_before_work(session):
    session.add(
        ProcessingRule(dimension="semantic_type", value="variety_clip", action="metadata_only")
    )
    session.commit()
    ingest(session, [CapturedSource(external_id="clip", title="综艺片段合集", caption="笑点很多")])
    source = session.scalar(select(Source).where(Source.external_id == "clip"))
    assert source.semantic_type == "variety_clip"
    assert source.status == "metadata_only"
    assert session.scalar(select(Job).where(Job.source_id == source.id)) is None
