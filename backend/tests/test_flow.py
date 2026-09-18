import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from douyin_knowledge.ai import ExtractedClaim, Extraction
from douyin_knowledge.api import jobs, resurface, wiki_page
from douyin_knowledge.capture import CapturedSource
from douyin_knowledge.db import Base
from douyin_knowledge.index import rebuild
from douyin_knowledge.models import (
    Claim,
    Entity,
    Evidence,
    Job,
    PolicyDecision,
    ProcessingRule,
    ProcessingRun,
    Source,
    SourceAnnotation,
    SourceAsset,
    SourceCollectionMembership,
    SourceSnapshot,
    SyncEvent,
    UserState,
    WikiPage,
    WikiQualityEvent,
    WikiRevision,
    WikiSupport,
)
from douyin_knowledge.policy import evaluate
from douyin_knowledge.retrieval import answer, plan, search
from douyin_knowledge.service import (
    _validated_claims,
    ingest,
    process_source,
    reevaluate,
    sync_capture,
    work_once,
)
from douyin_knowledge.wiki import audit, fix


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
    page_detail = wiki_page(page.id, session)
    assert page_detail["supports"]
    assert page_detail["supports"][0]["source_title"] == "旺角平价日料推荐"
    assert all(item["source_id"] for item in page_detail["supports"])
    assert any(item["source_title"] == "旺角平价日料推荐" for item in jobs(session))
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


def test_general_question_is_not_assumed_to_be_about_saves():
    assert plan("MCP 是什么？")["scope"] == "general"
    assert plan("我收藏的视频里大家怎么解释 MCP？")["scope"] == "personal"
    assert plan("结合我的收藏和一般知识解释 MCP")["scope"] == "hybrid"


def test_price_answer_does_not_cite_unrelated_dish_claim(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    result = answer(session, "我收藏的樱花食堂人均多少？")
    assert len(result["citations"]) == 1
    assert "80" in result["citations"][0]["text"]


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


def test_failed_job_exhausts_retries_and_new_capture_recovers(session):
    ingest(session, [CapturedSource(external_id="recover", title="Empty")])
    for attempt in range(3):
        assert work_once(session)
        job = session.scalar(select(Job).order_by(Job.id.desc()))
        assert job.attempts == attempt + 1
        if attempt < 2:
            job.available_at = job.locked_at
            session.commit()
    assert job.status == "failed"
    ingest(
        session,
        [
            CapturedSource(
                external_id="recover", title="Updated", caption="新增了足够详细的可引用描述。"
            )
        ],
    )
    assert work_once(session)
    source = session.scalar(select(Source).where(Source.external_id == "recover"))
    assert source.status == "ready"
    assert session.get(ProcessingRun, source.current_run_id).result_summary["evidence_count"] == 1


def test_sync_history_records_success_and_provider_failure(session):
    ingest(session, demo()[:1])

    class BrokenProvider:
        def list_saves(self):
            raise RuntimeError("sidecar unavailable")

    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        sync_capture(session, BrokenProvider(), "sidecar")
    events = session.scalars(select(SyncEvent).order_by(SyncEvent.occurred_at)).all()
    assert [event.status for event in events] == ["succeeded", "failed"]
    assert events[1].kind == "sidecar"


def test_claim_validation_rejects_empty_quote_and_unquoted_value(session):
    evidence = Evidence(source_id="source-id", kind="caption", text="这家店人均80港币")
    evidence.id = "evidence-id"
    extraction = Extraction(
        summary="",
        claims=[
            ExtractedClaim(evidence_id="evidence-id", quote="", value="100港币"),
            ExtractedClaim(evidence_id="evidence-id", quote="人均80港币", value="100港币"),
            ExtractedClaim(evidence_id="evidence-id", quote="人均80港币", value="80港币"),
        ],
    )
    assert [claim.value for claim in _validated_claims(extraction, [evidence])] == ["80港币"]


def test_collection_move_updates_active_policy_and_preserves_snapshot(session):
    session.add(ProcessingRule(dimension="collection", value="待看影视", action="metadata_only"))
    session.commit()
    original = demo()[0].model_copy(
        update={"collection": "待看影视", "collections": ["待看影视", "香港美食"]}
    )
    ingest(session, [original])
    source = session.scalar(select(Source))
    assert source.status == "metadata_only"
    moved = original.model_copy(update={"collection": "香港美食", "collections": ["香港美食"]})
    ingest(session, [moved])
    session.refresh(source)
    assert source.status == "pending"
    assert set(
        session.scalars(
            select(SourceCollectionMembership.collection_name).where(
                SourceCollectionMembership.source_id == source.id
            )
        ).all()
    ) == {"香港美食"}
    snapshots = session.scalars(
        select(SourceSnapshot).where(SourceSnapshot.source_id == source.id)
    ).all()
    assert any("待看影视" in snapshot.payload["collections"] for snapshot in snapshots)


def test_sidecar_removal_hides_claims_and_reappearance_restores_source(session):
    record = demo()[0]
    ingest(session, [record], sync_kind="sidecar")
    while work_once(session):
        pass
    source = session.scalar(select(Source))
    assert answer(session, "我收藏的樱花食堂人均多少？")["citations"]
    ingest(session, [], sync_kind="sidecar")
    session.refresh(source)
    assert source.status == "removed"
    assert answer(session, "我收藏的樱花食堂人均多少？")["citations"] == []
    assert audit(session) == []
    assert any(
        snapshot.payload.get("present") is False
        for snapshot in session.scalars(
            select(SourceSnapshot).where(SourceSnapshot.source_id == source.id)
        )
    )
    ingest(session, [record], sync_kind="sidecar")
    session.refresh(source)
    assert source.status == "ready"
    assert answer(session, "我收藏的樱花食堂人均多少？")["citations"]


def test_long_transcript_claim_remains_searchable(session):
    transcript = "普通内容。" * 100 + " 深海种植 技术值得了解。"
    ingest(
        session,
        [
            CapturedSource(
                external_id="long-asr",
                title="课程记录",
                transcript=transcript,
                claims=[{"quote": "深海种植", "value": "深海种植", "predicate": "states"}],
            )
        ],
    )
    while work_once(session):
        pass
    result = search(session, "深海种植")
    assert len(result) == 1
    assert "fts" in result[0]["methods"]


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


def test_changed_source_is_versioned_and_stale_claims_hidden(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    source = session.scalar(select(Source))
    first_run = source.current_run_id
    first_snapshot_count = len(session.scalars(select(SourceSnapshot)).all())
    updated = demo()[0].model_copy(
        update={
            "caption": "旺角的樱花食堂人均约120港币，推荐寿司。",
            "claims": [
                {
                    "quote": "樱花食堂人均约120港币",
                    "predicate": "average_price",
                    "value": "约120港币",
                    "entity_name": "樱花食堂",
                    "entity_kind": "restaurant",
                }
            ],
        }
    )
    ingest(session, [updated])
    session.refresh(source)
    assert source.status == "pending"
    assert len(session.scalars(select(SourceSnapshot)).all()) == first_snapshot_count + 1
    assert answer(session, "我收藏的樱花食堂人均多少？")["citations"] == []
    assert work_once(session)
    session.refresh(source)
    assert source.status == "ready"
    assert source.current_run_id != first_run
    assert session.scalar(select(Claim).where(Claim.run_id == first_run))
    assert any(
        "120" in c["text"] for c in answer(session, "我收藏的樱花食堂人均多少？")["citations"]
    )


def test_policy_decisions_are_audited(session):
    ingest(session, demo()[:1])
    source = session.scalar(select(Source))
    session.add(ProcessingRule(dimension="creator", value="food_creator", action="metadata_only"))
    session.commit()
    reevaluate(session)
    decisions = session.scalars(
        select(PolicyDecision).where(PolicyDecision.source_id == source.id)
    ).all()
    assert [row.action for row in decisions] == ["process", "metadata_only"]
    assert decisions[-1].rule_id


def test_media_transcription_retained_as_evidence(session, monkeypatch):
    monkeypatch.setattr(
        "douyin_knowledge.service.OpenAITranscriber.transcribe",
        lambda self, url: "这是一段从视频转写得到的详细内容。",
    )
    ingest(
        session,
        [
            CapturedSource(
                external_id="media", caption="短标题", media_url="https://example.com/a.mp4"
            )
        ],
    )
    source = session.scalar(select(Source).where(Source.external_id == "media"))
    assert session.scalar(select(SourceAsset).where(SourceAsset.source_id == source.id))
    process_source(session, source.id)
    assert source.status == "ready"
    assert (
        session.scalar(select(ProcessingRun).where(ProcessingRun.id == source.current_run_id)).level
        == 2
    )
    assert session.scalar(
        select(Evidence).where(Evidence.source_id == source.id, Evidence.kind == "asr")
    )


def test_media_failure_falls_back_to_caption(session, monkeypatch):
    def fail(self, url):
        raise RuntimeError("expired media URL")

    monkeypatch.setattr("douyin_knowledge.service.OpenAITranscriber.transcribe", fail)
    ingest(
        session,
        [
            CapturedSource(
                external_id="expired",
                caption="这段说明足以作为文本证据。",
                media_url="https://example.com/a.mp4",
            )
        ],
    )
    source = session.scalar(select(Source).where(Source.external_id == "expired"))
    process_source(session, source.id)
    run = session.get(ProcessingRun, source.current_run_id)
    assert run.status == "succeeded"
    assert "expired media URL" in run.error
    assert run.level == 1


def test_resurface_requires_current_evidence(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    entity = session.scalar(select(Entity).where(Entity.name == "樱花食堂"))
    session.add(UserState(entity_id=entity.id, state="want_to_go", note="周末看看"))
    session.commit()
    cards = resurface(session)
    assert cards[0]["entity_name"] == "樱花食堂"
    assert cards[0]["sources"][0]["claim_id"]
    source = session.scalar(select(Source))
    source.status = "metadata_only"
    session.commit()
    assert resurface(session) == []


def test_resync_clears_removed_fixture_annotations(session):
    record = demo()[0]
    ingest(session, [record])
    source = session.scalar(select(Source))
    assert session.get(SourceAnnotation, source.id).payload
    ingest(session, [record.model_copy(update={"claims": []})])
    assert session.get(SourceAnnotation, source.id).payload == []
    process_source(session, source.id)
    assert not session.scalar(
        select(Claim).where(Claim.run_id == source.current_run_id, Claim.entity_id.is_not(None))
    )


def test_wiki_quality_ledger_and_deterministic_repair(session):
    ingest(session, demo()[:1])
    while work_once(session):
        pass
    page = session.scalar(select(WikiPage))
    revision = session.scalar(
        select(WikiRevision).where(
            WikiRevision.page_id == page.id, WikiRevision.number == page.current_revision
        )
    )
    support = session.scalar(select(WikiSupport).where(WikiSupport.revision_id == revision.id))
    session.delete(support)
    session.commit()
    assert audit(session)[0]["issue"] == "missing_support"
    event = session.scalar(select(WikiQualityEvent))
    assert event.status == "open"
    result = fix(session)
    session.refresh(event)
    assert result["remaining"] == []
    assert page.current_revision == 2
    assert event.status == "resolved"


def test_running_job_is_not_claimed_by_second_worker(session, monkeypatch):
    ingest(session, demo()[:1])
    entered = threading.Event()
    release = threading.Event()

    def pause(_session, _source_id):
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr("douyin_knowledge.service.process_source", pause)
    outcome = []

    def first_worker():
        with Session(session.bind) as other:
            outcome.append(work_once(other))

    worker = threading.Thread(target=first_worker)
    worker.start()
    assert entered.wait(timeout=5)
    assert not work_once(session)
    release.set()
    worker.join(timeout=5)
    assert outcome == [True]
    job = session.scalar(select(Job))
    session.refresh(job)
    assert job.attempts == 1
