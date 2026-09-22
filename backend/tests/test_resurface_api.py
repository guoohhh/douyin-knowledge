"""Resurface: the user's saved intentions, and what may support them (P1-4).

Two separate invariants, and confusing them is the bug this file exists to prevent.

The *intention* is the user's own statement. Policy and currency must never remove it: a
rule about a creator's video is not a statement about what the user wants, and deleting a
saved "I want to go here" because a source was excluded would lose data the user authored.

The *support* on the card is knowledge, so it obeys the same eligibility rule as every
other normal knowledge surface -- current run, policy-eligible source, assertable claim.
A card whose only support is hidden shows the intention with no support rather than
silently presenting an excluded source's claims.

Built on `EntityUserState`, deliberately, not `KnowledgeItem`: that table has no writer, so
a feature resting on it could not work.
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

ENTITIES = "/api/knowledge/entities"
RESURFACE = "/api/knowledge/resurface"


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
    worker = Worker(register_default_handlers(), settings=settings, name="resurface-test")
    assert client.post("/api/sources/sync", json={"auto_process": True}).status_code == 202
    assert worker.drain(max_jobs=500) > 0
    return client


def _entity_with_claims(client: TestClient) -> str:
    entities = client.get(ENTITIES, params={"min_claims": 1, "limit": 200}).json()["entities"]
    assert entities, "the fixture corpus must produce at least one entity with claims"
    return max(entities, key=lambda e: e["claim_count"])["id"]


def _save(client: TestClient, entity_id: str, state: str, **extra: object) -> dict:
    response = client.put(
        f"{ENTITIES}/{entity_id}/user-state", json={"state": state, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _card(client: TestClient, entity_id: str, **params: object) -> dict | None:
    cards = client.get(RESURFACE, params=params).json()["cards"]
    return next((c for c in cards if c["entity_id"] == entity_id), None)


def _exclude_sources(client: TestClient, source_ids: set[str], action: str) -> None:
    """Apply a per-source rule to each id, the way the Settings UI would."""
    for source_id in sorted(source_ids):
        response = client.post(
            "/api/admin/policy/rules",
            json={
                "name": f"{action} {source_id}",
                "rule_type": "source",
                "action": action,
                "priority": 90,
                "is_enabled": True,
                "target_source_id": source_id,
            },
        )
        assert response.status_code == 201, response.text


class TestTheSavedIntentionLoop:
    """Entity detail → set state → it persists → Resurface lists it → clear it."""

    @pytest.mark.parametrize("state", ["want_to_go", "want_to_try", "want_to_learn"])
    def test_a_state_can_be_set_read_and_cleared(
        self, populated: TestClient, state: str
    ) -> None:
        entity_id = _entity_with_claims(populated)

        # Nothing saved yet: an explicit null, not a 404. "You have not saved this" is a
        # normal answer for an entity page to render.
        initial = populated.get(f"{ENTITIES}/{entity_id}/user-state")
        assert initial.status_code == 200
        assert initial.json()["state"] is None

        saved = _save(populated, entity_id, state, note="想去试试")
        assert saved["state"] == state
        assert saved["note"] == "想去试试"
        assert saved["first_action_at_ms"] is not None

        # Readable on the entity page itself, so the UI renders the control in one request.
        detail = populated.get(f"{ENTITIES}/{entity_id}").json()
        assert detail["user_state"]["state"] == state

        card = _card(populated, entity_id)
        assert card is not None, "a saved intention must appear on Resurface"
        assert card["state"] == state
        assert card["note"] == "想去试试"

        cleared = populated.delete(f"{ENTITIES}/{entity_id}/user-state")
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] is True
        assert cleared.json()["state"] is None
        assert _card(populated, entity_id) is None

    def test_clearing_keeps_the_note_the_user_wrote(self, populated: TestClient) -> None:
        """Withdrawing the intention is not withdrawing the reason for it."""
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_go", note="朋友推荐的")
        populated.delete(f"{ENTITIES}/{entity_id}/user-state")

        after = populated.get(f"{ENTITIES}/{entity_id}/user-state").json()
        assert after["state"] is None
        assert after["note"] == "朋友推荐的", "clearing a state must not delete the note"

    def test_resaving_updates_rather_than_duplicating(self, populated: TestClient) -> None:
        entity_id = _entity_with_claims(populated)
        first = _save(populated, entity_id, "want_to_go")
        second = _save(populated, entity_id, "want_to_try")

        assert second["state"] == "want_to_try"
        # first_action_at_ms answers "how long has this been on my list", so it must not move.
        assert second["first_action_at_ms"] == first["first_action_at_ms"]

        cards = populated.get(RESURFACE).json()["cards"]
        assert len([c for c in cards if c["entity_id"] == entity_id]) == 1

    def test_filtering_and_unknown_states(self, populated: TestClient) -> None:
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_learn")

        assert _card(populated, entity_id, state="want_to_learn") is not None
        assert _card(populated, entity_id, state="want_to_go") is None

        # A typo in the filter is a 422, not an empty list: silently returning nothing would
        # read as "you have saved nothing", which is a different and alarming statement.
        assert populated.get(RESURFACE, params={"state": "want_to_goo"}).status_code == 422
        bad_write = populated.put(
            f"{ENTITIES}/{entity_id}/user-state", json={"state": "want_to_eat"}
        )
        assert bad_write.status_code == 422

    def test_state_on_a_missing_entity_is_404(self, populated: TestClient) -> None:
        assert (
            populated.put(
                f"{ENTITIES}/ent_nope/user-state", json={"state": "want_to_go"}
            ).status_code
            == 404
        )
        assert populated.get(f"{ENTITIES}/ent_nope/user-state").status_code == 404


class TestCardProvenance:
    def test_each_card_links_back_to_entity_source_and_evidence(
        self, populated: TestClient
    ) -> None:
        """A card with no route back to the video is an unverifiable assertion."""
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_go")

        card = _card(populated, entity_id)
        assert card is not None
        assert card["has_eligible_support"] is True, "this entity has eligible claims"
        assert card["supports"], "support must be listed, not just counted"

        for support in card["supports"]:
            assert support["claim_id"]
            assert support["source_id"]
            # The source detail route is the provenance surface; it must resolve.
            source = populated.get(f"/api/sources/{support['source_id']}")
            assert source.status_code == 200, support["source_id"]

        # And the entity itself is reachable, which is where the full claim list lives.
        assert populated.get(f"{ENTITIES}/{entity_id}").status_code == 200


class TestPolicyAndCurrencyGovernSupportNotTheIntention:
    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_a_card_supported_only_by_a_hidden_source_shows_no_support(
        self, populated: TestClient, action: str
    ) -> None:
        """The intention survives; the hidden source's claims must not be presented."""
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_go", note="别丢了我这条")

        before = _card(populated, entity_id)
        assert before is not None and before["supports"]
        supporting = {s["source_id"] for s in before["supports"]}

        # Hide every source that currently supports this entity, not just the listed page:
        # capping supports per card means the list is a sample, and leaving an unlisted
        # source eligible would make this test pass for the wrong reason.
        detail = populated.get(f"{ENTITIES}/{entity_id}").json()
        supporting |= {c["source_id"] for c in detail["claims"]}
        _exclude_sources(populated, supporting, action)

        after = _card(populated, entity_id)
        assert after is not None, "a policy rule must not delete the user's own intention"
        assert after["state"] == "want_to_go"
        assert after["note"] == "别丢了我这条"
        assert after["has_eligible_support"] is False
        assert after["supports"] == [], f"{action} source must not support a card"

    def test_one_excluded_source_leaves_the_others_supporting(
        self, populated: TestClient
    ) -> None:
        """Partial exclusion narrows the support; it does not empty it."""
        entity_id = None
        for candidate in populated.get(
            ENTITIES, params={"min_claims": 1, "limit": 200}
        ).json()["entities"]:
            detail = populated.get(f"{ENTITIES}/{candidate['id']}").json()
            if len({c["source_id"] for c in detail["claims"]}) >= 2:
                entity_id = candidate["id"]
                break
        if entity_id is None:
            pytest.skip("fixture corpus has no entity supported by two sources")

        _save(populated, entity_id, "want_to_go")
        detail = populated.get(f"{ENTITIES}/{entity_id}").json()
        all_sources = sorted({c["source_id"] for c in detail["claims"]})
        _exclude_sources(populated, {all_sources[0]}, "exclude")

        card = _card(populated, entity_id)
        assert card is not None
        assert card["has_eligible_support"] is True, "eligible support must still be used"
        shown = {s["source_id"] for s in card["supports"]}
        assert all_sources[0] not in shown, "the excluded source must not appear"

    def test_reversal_restores_support_without_reprocessing(
        self, populated: TestClient
    ) -> None:
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_go")
        detail = populated.get(f"{ENTITIES}/{entity_id}").json()
        sources = {c["source_id"] for c in detail["claims"]}

        _exclude_sources(populated, sources, "exclude")
        assert _card(populated, entity_id)["supports"] == []

        rules = populated.get("/api/admin/policy/rules").json()["rules"]
        for rule in rules:
            if rule["target_source_id"] in sources:
                assert populated.delete(f"/api/admin/policy/rules/{rule['id']}").status_code == 200

        restored = _card(populated, entity_id)
        assert restored is not None
        assert restored["has_eligible_support"] is True, (
            "reversal must restore support from retained claims, with no reprocessing"
        )

    def test_a_downgraded_claim_does_not_support_a_card(
        self, populated: TestClient
    ) -> None:
        """Support is knowledge, so it obeys grounding too (DEC-017)."""
        entity_id = _entity_with_claims(populated)
        _save(populated, entity_id, "want_to_go")

        before = _card(populated, entity_id)
        assert before is not None and before["supports"]
        claim_ids = {s["claim_id"] for s in before["supports"]}

        with session_scope() as session:
            for claim in session.scalars(select(Claim).where(Claim.id.in_(claim_ids))):
                claim.grounding_status = "downgraded"

        after = _card(populated, entity_id)
        assert after is not None
        assert claim_ids.isdisjoint({s["claim_id"] for s in after["supports"]}), (
            "a claim that is no longer assertable must not be presented as support"
        )

        # Retained, not deleted -- audit surfaces still need it.
        with session_scope() as session:
            assert session.scalars(select(Claim).where(Claim.id.in_(claim_ids))).all()


class TestUserStateIsNotAClaim:
    def test_saving_an_intention_creates_no_claim(self, populated: TestClient) -> None:
        """The user's wish is not something a creator said (KM-003)."""
        entity_id = _entity_with_claims(populated)
        with session_scope() as session:
            before = session.scalars(select(Claim)).all()
            before_ids = {c.id for c in before}

        _save(populated, entity_id, "want_to_go", note="下周去")

        with session_scope() as session:
            after_ids = {c.id for c in session.scalars(select(Claim)).all()}
        assert after_ids == before_ids, "user state must never be written as a Claim"

        detail = populated.get(f"{ENTITIES}/{entity_id}").json()
        predicates = {c["predicate"] for c in detail["claims"]}
        assert not {p for p in predicates if "want" in p.lower()}
        # And it is not smuggled in as claim text either.
        assert all("下周去" not in (c["value_text"] or "") for c in detail["claims"])
