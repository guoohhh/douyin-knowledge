"""Discovery reports upstream metadata beside saved scope and local observations."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from douyin_knowledge.capture.discovery import discover_capture_targets
from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.capture.models import CapturedCollection
from douyin_knowledge.capture.scope import CAPTURE_SCOPE_KEY, CaptureScope, set_capture_scope
from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import CaptureUnavailable
from douyin_knowledge.db.models.capture import Collection, DefaultFavoritesSyncState
from douyin_knowledge.db.models.ops import AppSetting
from douyin_knowledge.db.models.policy import ProcessingRule


class DiscoveryProvider:
    def __init__(self, collections: list[CapturedCollection] | Exception) -> None:
        self.collections = collections
        self.list_calls = 0
        self.content_calls = 0

    def list_collections(self) -> list[CapturedCollection]:
        self.list_calls += 1
        if isinstance(self.collections, Exception):
            raise self.collections
        return self.collections

    def list_collection_sources(self, *args: object, **kwargs: object) -> None:
        self.content_calls += 1
        pytest.fail("discovery must not read collection contents")

    def list_default_favorite_sources(self, *args: object, **kwargs: object) -> None:
        self.content_calls += 1
        pytest.fail("discovery must not read default favorites contents")


def test_discovery_merges_selected_discovered_and_local_state_without_mutation(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_capture_scope(
        session,
        CaptureScope(default_favorites=True, named_collection_ids=["selected", "missing"]),
    )
    session.add_all([
        Collection(platform="douyin", external_collection_id="selected", name="Old name", last_synced_at_ms=123),
        Collection(platform="douyin", external_collection_id="local-only", name="Local only", last_synced_at_ms=456),
        DefaultFavoritesSyncState(platform="douyin", last_completed_at_ms=789),
        ProcessingRule(name="unchanged", rule_type="metadata", action="exclude"),
    ])
    session.flush()
    provider = DiscoveryProvider([
        CapturedCollection(external_collection_id="selected", name="Current name", item_count=9),
        CapturedCollection(external_collection_id="new", name="New", item_count=4),
    ])
    monkeypatch.setattr("douyin_knowledge.capture.discovery.get_capture_provider", lambda _: provider)

    result = discover_capture_targets(session, settings)
    assert result.discovery.state == "ok"
    assert result.default_favorites.available and result.default_favorites.selected
    assert result.default_favorites.last_completed_at_ms == 789
    targets = {item.external_collection_id: item for item in result.named_collections}
    assert set(targets) == {"selected", "missing", "local-only", "new"}
    assert targets["selected"].model_dump() == {
        "external_collection_id": "selected", "name": "Current name", "item_count": 9,
        "discovered": True, "locally_observed": True, "selected": True,
        "last_synced_at_ms": 123,
    }
    assert targets["missing"].selected and not targets["missing"].discovered
    assert not targets["missing"].locally_observed and targets["missing"].name is None
    assert targets["local-only"].locally_observed and not targets["local-only"].discovered
    assert targets["local-only"].last_synced_at_ms == 456
    assert targets["new"].discovered and not targets["new"].selected
    assert provider.list_calls == 1 and provider.content_calls == 0
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == result.scope.model_dump()
    assert session.query(ProcessingRule).count() == 1


def test_discovery_failure_preserves_empty_scope_and_local_state(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_capture_scope(session, CaptureScope())
    session.add(Collection(platform="douyin", external_collection_id="historic", name="Historic", last_synced_at_ms=321))
    session.flush()
    provider = DiscoveryProvider(CaptureUnavailable("synthetic private failure detail"))
    monkeypatch.setattr("douyin_knowledge.capture.discovery.get_capture_provider", lambda _: provider)

    result = discover_capture_targets(session, settings)
    assert result.discovery.state == "error"
    assert result.discovery.error_code == "capture_unavailable"
    assert "synthetic private failure detail" not in result.model_dump_json()
    assert result.scope == CaptureScope()
    assert result.named_collections[0].external_collection_id == "historic"
    assert not result.named_collections[0].discovered
    assert result.named_collections[0].last_synced_at_ms == 321
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == CaptureScope().model_dump()


def test_configured_fixture_discovery_uses_local_directory(
    session: Session, tmp_path: Path
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "collections.json").write_text(
        json.dumps([{"external_collection_id": "configured", "name": "Configured"}]),
        encoding="utf-8",
    )
    (root / "sources.json").write_text("[]", encoding="utf-8")
    settings = Settings(capture_provider="fixture", capture_fixture_dir=root, data_dir=tmp_path / "data")
    result = discover_capture_targets(session, settings)
    assert result.discovery.state == "ok"
    assert result.scope.named_collection_ids == ["configured"]
    assert result.named_collections[0].external_collection_id == "configured"
    assert result.named_collections[0].discovered


def test_real_douyin_discovery_uses_only_named_collection_endpoint(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.capture_provider = "douyin"
    set_capture_scope(session, CaptureScope())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "success": True,
            "data": {"items": [{"collection_id": "123", "name": "Discovered", "item_count": 2}],
                     "cursor": None, "has_more": False},
            "meta": {},
        })

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://sidecar.invalid"
    )
    provider = DouyinCaptureProvider(
        "http://sidecar.invalid", identity="local-identity", client=client
    )
    monkeypatch.setattr("douyin_knowledge.capture.discovery.get_capture_provider", lambda _: provider)
    result = discover_capture_targets(session, settings)
    assert result.discovery.state == "ok"
    assert result.named_collections[0].external_collection_id == "123"
    assert not result.named_collections[0].selected
    assert session.get(AppSetting, CAPTURE_SCOPE_KEY).value_json == CaptureScope().model_dump()
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/douyin/user/collections"
    assert requests[0].url.params["identity"] == "local-identity"
    assert client.is_closed
