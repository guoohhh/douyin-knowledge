"""Entity/Knowledge surfaces must honour the same eligibility rule as Ask and search.

The gap these cover: `HybridRetriever` and `WikiBuilder` filtered on policy and grounding,
but `/api/knowledge/entities` and its detail route filtered only on the processing-run pointer.
So excluding a source removed it from Ask and from search while its claims stayed on entity
pages and in claim counts -- the knowledge was hidden from the surfaces a user would test
and left visible on the ones they would browse.

Everything here runs through the real sync → process → index queue, because the behaviour
under test is what happens to knowledge that *already exists* when policy changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from douyin_knowledge.api import create_app
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.db import session_scope
from douyin_knowledge.db.models.entities import Claim
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
def client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


@pytest.fixture
def populated(client: TestClient, settings: Settings) -> TestClient:
    worker = Worker(register_default_handlers(), settings=settings, name="eligibility-test")
    assert client.post("/api/sources/sync", json={"auto_process": True}).status_code == 202
    assert worker.drain(max_jobs=500) > 0
    return client


def _entity_with_claims(client: TestClient) -> tuple[str, int]:
    """An entity that currently has claims, plus that count. Fails loudly if none."""
    entities = client.get("/api/knowledge/entities", params={"min_claims": 1, "limit": 200}).json()[
        "entities"
    ]
    assert entities, "the fixture corpus must produce at least one entity with claims"
    top = max(entities, key=lambda e: e["claim_count"])
    return top["id"], top["claim_count"]


def _claim_source_ids(client: TestClient, entity_id: str) -> set[str]:
    detail = client.get(f"/api/knowledge/entities/{entity_id}").json()
    return {c["source_id"] for c in detail["claims"]}


class TestPolicyHidesKnowledgeFromEntitySurfaces:
    """`exclude` and `metadata_only` must both take effect immediately, everywhere."""

    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_hidden_source_leaves_entity_detail_and_counts(
        self, populated: TestClient, action: str
    ) -> None:
        entity_id, count_before = _entity_with_claims(populated)
        sources = _claim_source_ids(populated, entity_id)
        assert sources, "entity must have claims from at least one source"
        target = sorted(sources)[0]

        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": action, "target_source_id": target},
        )
        assert created.status_code == 201, created.text
        assert created.json()["reconciled"]["now_hidden"] == 1

        # Entity detail: the hidden source must contribute no claims.
        assert target not in _claim_source_ids(populated, entity_id)

        # Entity list: the count must drop, not merely be recomputed identically.
        after = populated.get(
            "/api/knowledge/entities", params={"min_claims": 0, "limit": 200}
        ).json()["entities"]
        count_after = next(e["claim_count"] for e in after if e["id"] == entity_id)
        assert count_after < count_before, (
            f"{action} must reduce the visible claim count "
            f"(before={count_before}, after={count_after})"
        )

    def test_reversal_restores_claims_without_reprocessing(
        self, populated: TestClient
    ) -> None:
        """Deleting the rule brings the same claim ids back, with no new run."""
        entity_id, count_before = _entity_with_claims(populated)
        target = sorted(_claim_source_ids(populated, entity_id))[0]

        claim_ids_before = {
            c["id"]
            for c in populated.get(f"/api/knowledge/entities/{entity_id}").json()["claims"]
            if c["source_id"] == target
        }
        run_before = populated.get(f"/api/sources/{target}").json()["processing"][
            "current_processing_run_id"
        ]

        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "metadata_only", "target_source_id": target},
        ).json()
        assert target not in _claim_source_ids(populated, entity_id)

        reversed_ = populated.delete(f"/api/admin/policy/rules/{created['id']}")
        assert reversed_.status_code == 200
        assert reversed_.json()["reconciled"]["now_visible"] == 1

        detail = populated.get(f"/api/knowledge/entities/{entity_id}").json()
        claim_ids_after = {c["id"] for c in detail["claims"] if c["source_id"] == target}
        assert claim_ids_after == claim_ids_before, "reversal must restore the same claims"

        run_after = populated.get(f"/api/sources/{target}").json()["processing"][
            "current_processing_run_id"
        ]
        assert run_after == run_before, "reversal must not trigger reprocessing"

        count_after = next(
            e["claim_count"]
            for e in populated.get(
                "/api/knowledge/entities", params={"min_claims": 0, "limit": 200}
            ).json()["entities"]
            if e["id"] == entity_id
        )
        assert count_after == count_before

    def test_hidden_source_keeps_its_audit_trail(self, populated: TestClient) -> None:
        """Source detail is an audit surface and must still show the evidence."""
        entity_id, _ = _entity_with_claims(populated)
        target = sorted(_claim_source_ids(populated, entity_id))[0]

        populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "metadata_only", "target_source_id": target},
        )

        detail = populated.get(f"/api/sources/{target}").json()
        assert detail["evidence"], "a policy change must not delete evidence"
        assert detail["processing"]["status"] == "processed"


class TestGroundingHidesClaimsFromEntitySurfaces:
    """A downgraded claim stays in SQLite as the audit record but is not knowledge."""

    def test_downgraded_claim_is_retained_but_not_presented(
        self, populated: TestClient
    ) -> None:
        entity_id, count_before = _entity_with_claims(populated)
        detail = populated.get(f"/api/knowledge/entities/{entity_id}").json()
        assert detail["claims"], "entity must have claims"
        victim_id = detail["claims"][0]["id"]

        # Downgrade one claim the way the grounding validator would, in the database,
        # rather than through an API that does not exist -- the point of the test is the
        # read path, not how the column came to hold that value.
        with session_scope() as session:
            claim = session.get(Claim, victim_id)
            assert claim is not None
            claim.grounding_status = "downgraded"

        after = populated.get(f"/api/knowledge/entities/{entity_id}").json()
        assert victim_id not in {c["id"] for c in after["claims"]}, (
            "a downgraded claim must not be presented as knowledge"
        )

        count_after = next(
            e["claim_count"]
            for e in populated.get(
                "/api/knowledge/entities", params={"min_claims": 0, "limit": 200}
            ).json()["entities"]
            if e["id"] == entity_id
        )
        assert count_after == count_before - 1

        # Retained, not deleted: the row is still on disk.
        with session_scope() as session:
            assert session.get(Claim, victim_id) is not None
            still_there = session.scalars(
                select(Claim).where(Claim.id == victim_id)
            ).first()
            assert still_there is not None
            assert still_there.grounding_status == "downgraded"
