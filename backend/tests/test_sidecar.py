import httpx
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from douyin_knowledge.capture import SidecarCaptureProvider
from douyin_knowledge.db import Base
from douyin_knowledge.models import ProcessingRule, Source, SourceCollectionMembership
from douyin_knowledge.service import ingest


def test_douyin_sidecar_folders_pages_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("DK_SIDECAR_API_KEY", "test-key")
    monkeypatch.setenv("DK_SIDECAR_IDENTITY", "imported-identity")
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "test-key"
        assert request.url.params["identity"] == "imported-identity"
        path = request.url.path
        cursor = request.url.params.get("cursor")
        if path.endswith("/user/collections"):
            data = (
                {
                    "items": [{"collection_id": "a", "name": "学习"}],
                    "cursor": "next",
                    "has_more": True,
                }
                if not cursor
                else {
                    "items": [{"collection_id": "b", "name": "待看影视"}],
                    "cursor": None,
                    "has_more": False,
                }
            )
        elif path.endswith("/collection/posts"):
            data = {
                "items": [
                    {
                        "content_id": "123",
                        "web_url": "https://www.douyin.com/video/123",
                        "title": "MCP",
                        "description": "MCP 可以连接外部工具。",
                        "author": {"uid": "creator-1", "nickname": "作者"},
                    }
                ],
                "cursor": None,
                "has_more": False,
            }
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data, "error": None, "meta": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    records = SidecarCaptureProvider(client).list_saves()
    assert len(records) == 1
    assert records[0].collections == ["学习", "待看影视"]
    assert len(calls) == 4

    engine = create_engine(f"sqlite:///{tmp_path / 'sidecar.db'}")

    @event.listens_for(engine, "connect")
    def pragma(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE VIRTUAL TABLE search_fts USING fts5(source_id UNINDEXED, text)")
        )
    with Session(engine) as session:
        session.add(
            ProcessingRule(dimension="collection", value="待看影视", action="metadata_only")
        )
        session.commit()
        ingest(session, records)
        source = session.scalar(select(Source))
        assert source.status == "metadata_only"
        assert set(session.scalars(select(SourceCollectionMembership.collection_name)).all()) == {
            "学习",
            "待看影视",
        }
    engine.dispose()


def test_sidecar_async_task_result(monkeypatch):
    monkeypatch.setenv("DK_SIDECAR_API_KEY", "test-key")
    monkeypatch.setenv("DK_SIDECAR_IDENTITY", "imported-identity")
    monkeypatch.setattr("douyin_knowledge.capture.time.sleep", lambda _: None)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "state": "done",
                        "data": {"items": [], "cursor": None, "has_more": False},
                    },
                    "error": None,
                },
            )
        return httpx.Response(
            202,
            json={
                "success": True,
                "data": {"task_id": "task-1", "state": "running"},
                "error": None,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SidecarCaptureProvider(client)
    assert provider.list_saves() == []


def test_sidecar_failed_envelope_has_clear_error(monkeypatch):
    monkeypatch.setenv("DK_SIDECAR_API_KEY", "test-key")
    monkeypatch.setenv("DK_SIDECAR_IDENTITY", "imported-identity")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"success": False, "error": None})
        )
    )
    with pytest.raises(RuntimeError, match="Sidecar request failed: unknown"):
        SidecarCaptureProvider(client).list_saves()
