"""A policy change must reach the *current* wiki page immediately, not at the next rebuild.

The wiki is the one derived surface that persists its output. Retrieval and the Knowledge
API recompute from the spine on every read, so a policy filter there takes effect at once;
a committed `WikiRevision` keeps its text and its supports until something recompiles it.
Without this, excluding a source removed it from Ask and from entity pages while its
statement stayed on the wiki page as current knowledge, with a citation attached.

Historical revisions must survive: the contract is that history remains auditable and only
the *current* projection stops presenting the hidden source (AGENTS s4, DEC-015).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from douyin_knowledge.api import create_app
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.jobs.handlers import register_default_handlers
from douyin_knowledge.jobs.worker import Worker


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        ai_provider="mock",
        capture_provider="fixture",
        serve_frontend=False,
    )
    s.ensure_directories()
    return s


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    from douyin_knowledge.db import dispose_engine, init_engine
    from douyin_knowledge.db.migrate import upgrade_to_head

    upgrade_to_head(settings)
    eng = init_engine(settings, force=True)
    yield eng
    dispose_engine()


@pytest.fixture
def populated(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        worker = Worker(register_default_handlers(), settings=settings, name="wiki-policy-test")
        assert client.post("/api/sources/sync", json={"auto_process": True}).status_code == 202
        assert worker.drain(max_jobs=500) > 0
        yield client


def _page_supporting_a_source(client: TestClient) -> tuple[str, str]:
    """A current wiki page plus a source id its current revision actually cites."""
    pages = client.get("/api/knowledge/wiki").json()["pages"]
    assert pages, "the fixture corpus must build at least one wiki page"
    for page in pages:
        detail = client.get(f"/api/knowledge/wiki/{page['id']}").json()
        for support in detail["supports"]:
            resolved = support.get("resolved_source_id")
            if resolved:
                return page["id"], resolved
    raise AssertionError("no wiki page cites a resolvable source; the fixture cannot test this")


def _current_supported_sources(client: TestClient, page_id: str) -> set[str]:
    detail = client.get(f"/api/knowledge/wiki/{page_id}").json()
    if detail.get("revision") is None:
        return set()
    return {
        s["resolved_source_id"] for s in detail["supports"] if s.get("resolved_source_id")
    }


class TestPolicyChangeReachesCurrentWiki:
    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_hidden_source_stops_supporting_current_page(
        self, populated: TestClient, action: str
    ) -> None:
        page_id, source_id = _page_supporting_a_source(populated)
        assert source_id in _current_supported_sources(populated, page_id)

        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": action, "target_source_id": source_id},
        )
        assert created.status_code == 201, created.text
        assert created.json()["reconciled"]["now_hidden"] >= 1

        # Read the page immediately -- no rebuild call in between. This is the whole point.
        after = _current_supported_sources(populated, page_id)
        assert source_id not in after, (
            f"{action} must stop the source supporting current wiki knowledge immediately"
        )

    def test_history_survives_the_policy_change(self, populated: TestClient) -> None:
        page_id, source_id = _page_supporting_a_source(populated)
        revisions_before = populated.get(f"/api/knowledge/wiki/{page_id}/revisions").json()[
            "revisions"
        ]
        assert revisions_before

        populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        )

        revisions_after = populated.get(f"/api/knowledge/wiki/{page_id}/revisions").json()[
            "revisions"
        ]
        ids_before = {r["id"] for r in revisions_before}
        assert ids_before <= {r["id"] for r in revisions_after}, (
            "historical revisions must never be deleted by a policy change"
        )

    def test_reversal_restores_current_wiki_knowledge(self, populated: TestClient) -> None:
        page_id, source_id = _page_supporting_a_source(populated)
        before = _current_supported_sources(populated, page_id)

        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        ).json()
        assert source_id not in _current_supported_sources(populated, page_id)

        assert populated.delete(f"/api/admin/policy/rules/{created['id']}").status_code == 200

        restored = _current_supported_sources(populated, page_id)
        assert source_id in restored, "reversal must let the source support current wiki again"
        assert restored == before
