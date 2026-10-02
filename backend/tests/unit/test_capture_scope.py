"""Capture Scope persistence, provenance, and independently retryable targets."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider
from douyin_knowledge.capture.models import CapturedCollection, CapturedSource, SourcePage
from douyin_knowledge.capture.scope import (
    CAPTURE_SCOPE_KEY,
    CaptureScope,
    get_capture_scope,
    initialize_capture_scope,
    set_capture_scope,
)
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import AuthenticationRequired, CaptureUnavailable, ValidationError
from douyin_knowledge.db import session_scope
from douyin_knowledge.db.models.capture import (
    Collection,
    DefaultFavoritesSyncState,
    Source,
    SourceCollectionMembership,
    SourceDefaultFavoriteObservation,
)
from douyin_knowledge.db.models.ops import AppSetting, Job, JobEvent
from douyin_knowledge.db.models.policy import PolicyDecision, ProcessingRule, SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit
from douyin_knowledge.jobs.handlers import (
    handle_sync_capture_scope,
    handle_sync_named_collection,
    register_default_handlers,
)
from douyin_knowledge.jobs.queue import JobQueue
from douyin_knowledge.jobs.registry import JobContext
from douyin_knowledge.jobs.types import JobStatus, JobType
from douyin_knowledge.jobs.worker import Worker


def source(external_id: str) -> CapturedSource:
    return CapturedSource(platform="douyin", external_id=external_id, title=external_id)


class Pages:
    platform = "douyin"

    def __init__(self, pages: list[SourcePage | Exception]) -> None:
        self.pages = pages
        self.calls = 0
        self.on_page: Callable[[], None] | None = None

    def _next(self) -> SourcePage:
        index = self.calls
        self.calls += 1
        if self.on_page is not None:
            self.on_page()
        result = self.pages[index]
        if isinstance(result, Exception):
            raise result
        return result

    def list_default_favorite_sources(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        return self._next()

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        return self._next()


def test_bootstrap_existing_rows_once_and_round_trip(session: Session) -> None:
    session.add(Collection(platform="douyin", external_collection_id="named-a", name="A"))
    session.flush()
    first = get_capture_scope(session)
    assert first == CaptureScope(default_favorites=False, named_collection_ids=["named-a"])
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY) is not None

    session.add(Collection(platform="douyin", external_collection_id="named-b", name="B"))
    session.flush()
    assert get_capture_scope(session) == first

    chosen = CaptureScope(default_favorites=True, named_collection_ids=["named-b", "named-b"])
    assert set_capture_scope(session, chosen).named_collection_ids == ["named-b"]
    session.expire_all()
    assert get_capture_scope(session) == CaptureScope(
        default_favorites=True, named_collection_ids=["named-b"]
    )


def test_fresh_bootstrap_is_explicit_empty_scope(session: Session) -> None:
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY) is None
    assert get_capture_scope(session) == CaptureScope()
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == CaptureScope().model_dump()


def test_fresh_packaged_fixture_scope_uses_actual_provider(session: Session, settings: Settings) -> None:
    expected = [item.external_collection_id for item in FixtureCaptureProvider().list_collections()]
    scope = initialize_capture_scope(session, settings)
    assert scope == CaptureScope(default_favorites=False, named_collection_ids=expected)
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == scope.model_dump()


def test_custom_fixture_dir_bootstrap_uses_configured_provider(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_dir = tmp_path / "custom-fixture"
    fixture_dir.mkdir()
    (fixture_dir / "collections.json").write_text(
        json.dumps([{"external_collection_id": "custom-only", "name": "Custom"}]),
        encoding="utf-8",
    )
    (fixture_dir / "sources.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DK_CAPTURE_FIXTURE_DIR", str(fixture_dir))
    settings = Settings(_env_file=None)
    assert settings.capture_fixture_dir == fixture_dir
    assert initialize_capture_scope(session, settings).named_collection_ids == ["custom-only"]


def test_existing_rows_win_over_fixture_provider(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(Collection(platform="douyin", external_collection_id="legacy", name="Legacy"))
    session.flush()
    monkeypatch.setattr(
        "douyin_knowledge.capture.registry.get_capture_provider",
        lambda _: pytest.fail("existing rows must avoid fixture provider reads"),
    )
    assert initialize_capture_scope(session, settings).named_collection_ids == ["legacy"]


def test_explicit_empty_fixture_scope_stays_empty(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_capture_scope(session, CaptureScope())
    monkeypatch.setattr(
        "douyin_knowledge.capture.registry.get_capture_provider",
        lambda _: pytest.fail("explicit empty scope must avoid fixture provider reads"),
    )
    assert initialize_capture_scope(session, settings) == CaptureScope()
    queue = JobQueue(session)
    job = queue.enqueue(JobType.SYNC_CAPTURE_SCOPE, payload={}).job
    handle_sync_capture_scope(JobContext(job, session, queue, settings))
    assert session.query(Job).filter(Job.job_type == JobType.SYNC_NAMED_COLLECTION).count() == 0
    assert initialize_capture_scope(session, settings) == CaptureScope()


def test_fresh_real_douyin_scope_never_reads_provider(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.capture_provider = "douyin"
    monkeypatch.setattr(
        "douyin_knowledge.capture.registry.get_capture_provider",
        lambda _: pytest.fail("real Douyin bootstrap must not read provider"),
    )
    assert initialize_capture_scope(session, settings) == CaptureScope()
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == CaptureScope().model_dump()


def test_default_provider_uses_distinct_sidecar_route_and_paginates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor")
        item = {
            "content_id": "7001", "kind": "video", "title": "One", "description": "",
            "web_url": "https://www.douyin.com/video/7001",
            "author": {"uid": "author", "nickname": "Author"}, "media": {}, "stats": {},
        }
        if cursor is None:
            data = {"items": [item], "cursor": "next", "has_more": True}
        else:
            data = {"items": [item], "cursor": None, "has_more": False}
        return httpx.Response(200, json={"success": True, "data": data, "meta": {}})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://sidecar.invalid",
        headers={"X-API-Key": "synthetic-secret-key"},
    )
    provider = DouyinCaptureProvider(
        "http://sidecar.invalid", api_key="synthetic-secret-key",
        identity="pinned-id", client=client,
    )
    first = provider.list_default_favorite_sources(limit=5)
    second = provider.list_default_favorite_sources(cursor=first.next_cursor, limit=5)
    assert [item.external_id for item in first.sources] == ["7001"]
    assert first.has_more and first.next_cursor == "next"
    assert not second.has_more and second.next_cursor is None
    assert [request.url.path for request in seen] == [
        "/api/v1/douyin/user/bookmarks", "/api/v1/douyin/user/bookmarks"
    ]
    assert seen[0].url.params["identity"] == seen[1].url.params["identity"] == "pinned-id"
    assert seen[1].url.params["cursor"] == "next"
    assert all(request.url.params["count"] == "5" for request in seen)
    assert seen[0].headers["X-API-Key"] == "synthetic-secret-key"
    assert "synthetic-secret-key" not in caplog.text
    assert "pinned-id" not in caplog.text


def test_default_provider_refuses_missing_identity_without_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    provider = DouyinCaptureProvider(
        "http://sidecar.invalid",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sidecar.invalid"),
    )
    with pytest.raises(AuthenticationRequired):
        provider.list_default_favorite_sources()
    assert requests == 0


@pytest.mark.parametrize(
    ("cursor", "page"),
    [
        (None, {"items": [], "has_more": True, "cursor": None}),
        ("same", {"items": [], "has_more": True, "cursor": "same"}),
        (None, {"items": [], "has_more": "yes", "cursor": "next"}),
    ],
)
def test_default_provider_rejects_unsafe_pagination(cursor: str | None, page: dict) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": True, "data": page, "meta": {}})
        ),
        base_url="http://sidecar.invalid",
    )
    provider = DouyinCaptureProvider("http://sidecar.invalid", identity="pinned-id", client=client)
    with pytest.raises(ValidationError):
        provider.list_default_favorite_sources(cursor=cursor)


def test_overlap_idempotency_and_independent_memberships(session: Session) -> None:
    service = CaptureSyncService(session)
    a = CapturedCollection(external_collection_id="a", name="A")
    b = CapturedCollection(external_collection_id="b", name="B")
    only = SourcePage(sources=[source("same")], has_more=False)
    service.sync_default_favorites(Pages([only]))
    service.sync_collection(Pages([only]), a)
    service.sync_collection(Pages([only]), b)
    session.flush()
    assert session.query(Source).count() == 1
    assert session.query(SourceDefaultFavoriteObservation).count() == 1
    assert session.query(SourceCollectionMembership).count() == 2
    assert {row.is_present for row in session.scalars(select(SourceCollectionMembership))} == {1}

    repeated = service.sync_default_favorites(Pages([only]))
    service.sync_collection(Pages([only]), a)
    assert repeated.sources_created == 0
    assert repeated.snapshots == 0
    assert session.query(Source).count() == 1

    service.sync_collection(Pages([SourcePage(sources=[], has_more=False)]), a)
    memberships = {
        collection.external_collection_id: membership.is_present
        for membership, collection in session.execute(
            select(SourceCollectionMembership, Collection).join(
                Collection, Collection.id == SourceCollectionMembership.collection_id
            )
        )
    }
    assert memberships == {"a": 0, "b": 1}
    assert session.scalars(select(SourceDefaultFavoriteObservation)).one().is_present == 1


def test_complete_default_walk_reconciles_but_partial_preserves(session: Session) -> None:
    service = CaptureSyncService(session)
    original = SourcePage(sources=[source("a"), source("b")], has_more=False)
    service.sync_default_favorites(Pages([original]))
    first_completed = session.get(DefaultFavoritesSyncState, "douyin").last_completed_at_ms
    assert first_completed is not None

    partial = Pages([
        SourcePage(sources=[source("a")], next_cursor="page-2", has_more=True),
        CaptureUnavailable("temporary failure"),
    ])
    with pytest.raises(CaptureUnavailable):
        service.sync_default_favorites(partial)
    session.flush()
    assert {row.is_present for row in session.scalars(select(SourceDefaultFavoriteObservation))} == {1}
    assert session.get(DefaultFavoritesSyncState, "douyin").last_completed_at_ms == first_completed

    service.sync_default_favorites(Pages([SourcePage(sources=[source("a")], has_more=False)]))
    observations = {
        item.external_id: observation.is_present
        for item in session.scalars(
            select(Source).join(SourceDefaultFavoriteObservation)
        )
        for observation in [session.get(SourceDefaultFavoriteObservation, item.id)]
        if observation is not None
    }
    assert observations == {"a": 1, "b": 0}
    assert session.query(Source).count() == 2


def test_scope_deselection_preserves_source_evidence_and_policy(session: Session) -> None:
    service = CaptureSyncService(session)
    service.sync_default_favorites(Pages([SourcePage(sources=[source("keep")])]))
    item = session.scalars(select(Source)).one()
    rule = ProcessingRule(name="keep policy", rule_type="source", action="exclude", target_source_id=item.id)
    session.add(rule)
    session.flush()
    decision = PolicyDecision(source_id=item.id, rule_id=rule.id, phase="metadata", action="exclude")
    session.add(decision)
    session.flush()
    session.add(SourceProcessingState(
        source_id=item.id, current_policy_action="exclude", current_policy_decision_id=decision.id
    ))
    session.add(EvidenceUnit(source_id=item.id, kind="caption", content_hash="evidence", raw_text="saved"))
    set_capture_scope(session, CaptureScope(default_favorites=False))
    session.flush()
    assert session.get(Source, item.id) is not None
    assert session.query(EvidenceUnit).count() == 1
    assert session.query(ProcessingRule).count() == 1
    assert session.query(PolicyDecision).count() == 1
    assert session.get(SourceProcessingState, item.id).current_policy_action == "exclude"
    assert session.get(SourceDefaultFavoriteObservation, item.id).is_present == 1


def _fixture_ids() -> list[str]:
    return [item.external_collection_id for item in FixtureCaptureProvider().list_collections()]


def test_scope_job_fans_out_with_independent_dedupe_keys(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "douyin_knowledge.jobs.handlers.get_capture_provider",
        lambda _: pytest.fail("scope fan-out must not construct a provider"),
    )
    ids = _fixture_ids()[:2]
    set_capture_scope(session, CaptureScope(default_favorites=True, named_collection_ids=ids))
    queue = JobQueue(session)
    root = queue.enqueue(JobType.SYNC_CAPTURE_SCOPE, dedupe_key="scope", payload={"auto_process": False})
    handle_sync_capture_scope(JobContext(root.job, session, queue, settings))
    jobs = session.scalars(select(Job).where(Job.id != root.job.id)).all()
    assert len(jobs) == 3
    assert {job.dedupe_key for job in jobs} == {
        "sync_default_favorites:douyin",
        *(f"sync_named_collection:douyin:{item}" for item in ids),
    }
    handle_sync_capture_scope(JobContext(root.job, session, queue, settings))
    assert session.query(Job).count() == 4


def test_legacy_fixture_collection_is_reused_after_bootstrap(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FixtureCaptureProvider()
    captured = provider.list_collections()[0]
    session.add(Collection(
        platform="fixture", external_collection_id=captured.external_collection_id,
        name=captured.name,
    ))
    session.flush()
    assert captured.external_collection_id in get_capture_scope(
        session, provider_kind="fixture"
    ).named_collection_ids
    monkeypatch.setattr("douyin_knowledge.jobs.handlers.get_capture_provider", lambda _: provider)
    queue = JobQueue(session)
    job = queue.enqueue(
        JobType.SYNC_NAMED_COLLECTION,
        payload={"external_collection_id": captured.external_collection_id, "auto_process": False},
    ).job
    handle_sync_named_collection(JobContext(job, session, queue, settings))
    assert session.query(Collection).count() == 1


def test_deselected_target_skips_without_provider_reads(
    engine: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FixtureCaptureProvider()
    calls = 0

    def counted() -> list[CapturedCollection]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(provider, "list_collections", counted)
    monkeypatch.setattr(provider, "list_default_favorite_sources", lambda **_: counted())
    monkeypatch.setattr("douyin_knowledge.jobs.handlers.get_capture_provider", lambda _: provider)
    target = _fixture_ids()[0]
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=[target]))
        JobQueue(session).enqueue(
            JobType.SYNC_NAMED_COLLECTION,
            payload={"external_collection_id": target, "auto_process": False},
            dedupe_key=f"sync_named_collection:douyin:{target}",
        )
        JobQueue(session).enqueue(JobType.SYNC_DEFAULT_FAVORITES, payload={"auto_process": False})
        set_capture_scope(session, CaptureScope())
    worker = Worker(register_default_handlers(), settings=settings)
    assert worker.run_once()
    assert worker.run_once()
    with session_scope() as session:
        jobs = session.scalars(select(Job)).all()
        assert len(jobs) == 2
        for job in jobs:
            events = session.scalars(select(JobEvent).where(JobEvent.job_id == job.id)).all()
            assert job.status == JobStatus.SUCCEEDED
            assert "skipped" in {event.event_type for event in events}
    assert calls == 0


def test_deselected_during_walk_finishes_and_reconciles(
    engine: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _fixture_ids()[0]
    provider = FixtureCaptureProvider()
    captured = provider.list_collections()[0]
    first_source = provider.list_collection_sources(target, limit=1).sources[0]
    calls = 0

    class DuringWalk:
        platform = "douyin"

        def list_collections(self) -> list[CapturedCollection]:
            return [captured]

        def list_collection_sources(
            self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
        ) -> SourcePage:
            nonlocal calls
            calls += 1
            if cursor is None:
                return SourcePage(sources=[first_source], next_cursor="next", has_more=True)
            return SourcePage(sources=[], has_more=False)

    target_provider = DuringWalk()
    monkeypatch.setattr("douyin_knowledge.jobs.handlers.get_capture_provider", lambda _: target_provider)
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=[target]))
        JobQueue(session).enqueue(
            JobType.SYNC_NAMED_COLLECTION,
            payload={"external_collection_id": target, "auto_process": False},
        )

    def handler(ctx: object) -> None:
        target_provider.list_collection_sources_original = target_provider.list_collection_sources

        def deselect(external_collection_id: str, *, cursor: str | None = None, limit: int = 50) -> SourcePage:
            result = target_provider.list_collection_sources_original(
                external_collection_id, cursor=cursor, limit=limit
            )
            if cursor is None:
                set_capture_scope(ctx.session, CaptureScope())
            return result

        target_provider.list_collection_sources = deselect
        handle_sync_named_collection(ctx)

    registry = register_default_handlers()
    registry.register(JobType.SYNC_NAMED_COLLECTION, handler)
    assert Worker(registry, settings=settings).run_once()
    with session_scope() as session:
        assert get_capture_scope(session).named_collection_ids == []
        assert session.query(SourceCollectionMembership).count() == 1
        assert session.scalars(select(Collection)).one().last_synced_at_ms is not None
    assert calls == 2


def test_single_target_auto_process_enqueues_normal_policy_job(
    engine: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _fixture_ids()[0]
    provider = FixtureCaptureProvider()
    monkeypatch.setattr("douyin_knowledge.jobs.handlers.get_capture_provider", lambda _: provider)
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=[target]))
        JobQueue(session).enqueue(
            JobType.SYNC_NAMED_COLLECTION,
            payload={"external_collection_id": target, "auto_process": True},
        )
    assert Worker(register_default_handlers(), settings=settings).run_once()
    with session_scope() as session:
        assert session.scalar(select(Job).where(Job.job_type == JobType.PROCESS_SOURCE)) is not None
        assert session.query(PolicyDecision).count() == 0


def test_target_failure_does_not_rerun_completed_sibling(
    engine: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _fixture_ids()[:2]
    provider = FixtureCaptureProvider()
    original = provider.list_collection_sources
    calls: dict[str, int] = {item: 0 for item in ids}

    def flaky(external_collection_id: str, *, cursor: str | None = None, limit: int = 50) -> SourcePage:
        calls[external_collection_id] += 1
        if external_collection_id == ids[0] and calls[external_collection_id] == 1:
            raise CaptureUnavailable("temporary")
        return original(external_collection_id, cursor=cursor, limit=limit)

    monkeypatch.setattr(provider, "list_collection_sources", flaky)
    monkeypatch.setattr("douyin_knowledge.jobs.handlers.get_capture_provider", lambda _: provider)
    with session_scope() as session:
        set_capture_scope(session, CaptureScope(named_collection_ids=ids))
        JobQueue(session).enqueue(JobType.SYNC_CAPTURE_SCOPE, payload={"auto_process": False})
    Worker(register_default_handlers(), settings=settings).drain(max_jobs=10)
    with session_scope() as session:
        jobs = {job.payload_json.get("external_collection_id"): job for job in session.scalars(
            select(Job).where(Job.job_type == JobType.SYNC_NAMED_COLLECTION)
        )}
        assert jobs[ids[0]].status == JobStatus.QUEUED
        assert jobs[ids[1]].status == JobStatus.SUCCEEDED
        assert jobs[ids[1]].attempt == 1
        assert jobs[ids[0]].last_error_json is not None
        jobs[ids[0]].available_at_ms = 0
    assert Worker(register_default_handlers(), settings=settings).run_once()
    with session_scope() as session:
        jobs = {job.payload_json.get("external_collection_id"): job for job in session.scalars(
            select(Job).where(Job.job_type == JobType.SYNC_NAMED_COLLECTION)
        )}
        assert jobs[ids[0]].status == JobStatus.SUCCEEDED
        assert jobs[ids[0]].attempt == 2
        assert jobs[ids[1]].attempt == 1
    assert calls[ids[0]] == 2
    assert calls[ids[1]] == 1
