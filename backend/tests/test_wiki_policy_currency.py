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
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from douyin_knowledge.api import create_app
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.db import session_scope
from douyin_knowledge.db.models.entities import Claim, Entity, EntityMention
from douyin_knowledge.db.models.knowledge import Topic
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.wiki import WikiPage, WikiSupport
from douyin_knowledge.jobs.handlers import register_default_handlers
from douyin_knowledge.jobs.worker import Worker
from douyin_knowledge.wiki.builder import WikiBuilder


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


def _eligible_claims_by_source(db: Session) -> dict[str, list[Claim]]:
    """Grounded claims from each source's current run, grouped by source.

    The eligibility rule is duplicated here on purpose rather than imported: these tests
    exist to check that the production path filters correctly, so deriving the fixture from
    the same helper under test would make a broken filter agree with itself.
    """
    rows = db.scalars(
        select(Claim)
        .join(
            SourceProcessingState,
            (SourceProcessingState.source_id == Claim.source_id)
            & (SourceProcessingState.current_processing_run_id == Claim.processing_run_id),
        )
        .where(Claim.grounding_status == "valid")
        .order_by(Claim.id)
    ).all()
    grouped: dict[str, list[Claim]] = {}
    for claim in rows:
        grouped.setdefault(claim.source_id, []).append(claim)
    return grouped


def _hide(client: TestClient, source_id: str, action: str) -> str:
    created = client.post(
        "/api/admin/policy/rules",
        json={"rule_type": "source", "action": action, "target_source_id": source_id},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _revision_ids(client: TestClient, page_id: str) -> set[str]:
    body = client.get(f"/api/knowledge/wiki/{page_id}/revisions").json()
    return {r["id"] for r in body["revisions"]}


def _supports_of_revision(db: Session, revision_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(WikiSupport)
            .where(WikiSupport.wiki_revision_id == revision_id)
        )
        or 0
    )


def _is_active_with_current_revision(client: TestClient, page_id: str) -> bool:
    detail = client.get(f"/api/knowledge/wiki/{page_id}")
    if detail.status_code == 404:
        return False
    body = detail.json()
    return body["status"] == "active" and body.get("revision") is not None


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


class TestAPageWithNoEligibleSupportStopsBeingCurrent:
    """P1-2. A targeted rebuild that finds zero eligible claims must retire the page.

    `skipped_reason="no_current_claims"` was the whole response before: correct for a
    subject that never had a page, silently wrong for one that did. The old current
    revision stayed active and went on presenting knowledge whose only source the user had
    just hidden -- the exact failure the *shorter page* case already prevented, but at the
    boundary where the page becomes empty rather than smaller.

    History is the other half of the contract. Retiring is two flag writes; a fix that
    deleted the old revision or its supports would pass "the page is not current" and
    destroy the audit trail, so every test here asserts the rows survive.
    """

    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_entity_page_with_unique_support_is_archived_then_restored(
        self, populated: TestClient, action: str
    ) -> None:
        # A private entity supported by exactly one source, so hiding that source takes the
        # page's eligible claim set to zero. Reusing a fixture entity would not: the fixture
        # corpus shares entities across sources, which is the *partial* case below.
        with session_scope() as db:
            by_source = _eligible_claims_by_source(db)
            source_id, claims = next(iter(by_source.items()))
            claim = claims[0]
            entity = Entity(
                entity_type="concept",
                canonical_name="唯一来源实体",
                normalized_name="唯一来源实体",
            )
            db.add(entity)
            db.flush()
            claim.subject_entity_id = entity.id
            db.add(
                EntityMention(
                    source_id=claim.source_id,
                    processing_run_id=claim.processing_run_id,
                    mention_text=entity.canonical_name,
                    normalized_text=entity.normalized_name,
                    entity_type_hint=entity.entity_type,
                    resolved_entity_id=entity.id,
                    resolution_status="resolved",
                )
            )
            outcome = WikiBuilder(db).build_entity_page(entity)
            assert outcome.page_id
            page_id = outcome.page_id

        assert source_id in _current_supported_sources(populated, page_id)
        revisions_before = _revision_ids(populated, page_id)
        original_revision = populated.get(f"/api/knowledge/wiki/{page_id}").json()["revision"]["id"]
        with session_scope() as db:
            supports_before = _supports_of_revision(db, original_revision)
        assert supports_before > 0

        rule_id = _hide(populated, source_id, action)

        assert not _is_active_with_current_revision(populated, page_id), (
            f"{action} left an entity page active with a revision it can no longer support"
        )
        assert _current_supported_sources(populated, page_id) == set()

        # History survived: same revision ids, same support rows on the demoted revision.
        assert revisions_before <= _revision_ids(populated, page_id)
        with session_scope() as db:
            assert _supports_of_revision(db, original_revision) == supports_before, (
                "retiring a page must not delete the historical revision's supports"
            )

        assert populated.delete(f"/api/admin/policy/rules/{rule_id}").status_code == 200

        assert _is_active_with_current_revision(populated, page_id), (
            "reversing the rule must make the page current again without reprocessing"
        )
        assert source_id in _current_supported_sources(populated, page_id)
        after = _revision_ids(populated, page_id)
        assert revisions_before <= after, "restoring must append, never rewrite history"


class TestPolicyReprojectionReachesTopicPages:
    """P1-1. Reprojection used to find affected subjects through `EntityMention` only.

    Mentions name entities and nothing else, so a `Topic` page composed from a hidden
    source's claims was unreachable: the claim dropped out of the eligibility rule while
    the topic page kept presenting it as current, with a citation. `Topic` is a live page
    family with a real writer, so it has persisted output that can go stale.

    The fixture extractor produces entity claims, so these tests attach topics to claims
    that are already grounded and current. That is the smallest valid topic projection and
    it exercises the same persisted wiki path as any future topic extraction would.
    """

    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_topic_with_unique_support_is_archived_but_stays_auditable(
        self, populated: TestClient, action: str
    ) -> None:
        with session_scope() as db:
            by_source = _eligible_claims_by_source(db)
            source_id, claims = next(iter(by_source.items()))
            topic = Topic(name="单一来源主题", normalized_name="单一来源主题")
            db.add(topic)
            db.flush()
            claims[0].subject_topic_id = topic.id
            outcome = WikiBuilder(db).build_topic_page(topic)
            assert outcome.page_id
            page_id = outcome.page_id
            topic_id = topic.id

        assert source_id in _current_supported_sources(populated, page_id)
        revisions_before = _revision_ids(populated, page_id)
        original_revision = populated.get(f"/api/knowledge/wiki/{page_id}").json()["revision"]["id"]
        with session_scope() as db:
            supports_before = _supports_of_revision(db, original_revision)

        rule_id = _hide(populated, source_id, action)

        assert not _is_active_with_current_revision(populated, page_id), (
            f"{action} must stop a topic page being current knowledge, not only an entity one"
        )

        # The correction to the audit's assertion: current projection is gone, provenance
        # is not. Old revisions and their supports stay queryable as history (AGENTS s4).
        assert revisions_before <= _revision_ids(populated, page_id), (
            "archiving a topic page must never delete its historical revisions"
        )
        with session_scope() as db:
            assert _supports_of_revision(db, original_revision) == supports_before, (
                "historical WikiSupport rows are provenance and must survive the policy change"
            )
            page = db.get(WikiPage, page_id)
            assert page is not None and page.topic_id == topic_id
            assert page.status == "archived"

        assert populated.delete(f"/api/admin/policy/rules/{rule_id}").status_code == 200

        assert _is_active_with_current_revision(populated, page_id)
        assert source_id in _current_supported_sources(populated, page_id)
        assert revisions_before <= _revision_ids(populated, page_id)

    @pytest.mark.parametrize("action", ["exclude", "metadata_only"])
    def test_topic_with_other_eligible_support_stays_active_and_drops_the_hidden_source(
        self, populated: TestClient, action: str
    ) -> None:
        """The page gets shorter, not archived. This is the common case and the sharper one:
        archiving here would destroy knowledge the user never asked to hide."""
        with session_scope() as db:
            by_source = _eligible_claims_by_source(db)
            pairs = [(sid, claims[0]) for sid, claims in by_source.items()]
            assert len(pairs) >= 2, "the fixture corpus must ground claims in two sources"
            (source_a, claim_a), (source_b, claim_b) = pairs[0], pairs[1]
            topic = Topic(name="双来源主题", normalized_name="双来源主题")
            db.add(topic)
            db.flush()
            claim_a.subject_topic_id = topic.id
            claim_b.subject_topic_id = topic.id
            outcome = WikiBuilder(db).build_topic_page(topic)
            assert outcome.page_id
            page_id = outcome.page_id

        before = _current_supported_sources(populated, page_id)
        assert {source_a, source_b} <= before

        _hide(populated, source_a, action)

        assert _is_active_with_current_revision(populated, page_id), (
            "a topic that still has eligible support must stay current, not be archived"
        )
        after = _current_supported_sources(populated, page_id)
        assert source_a not in after, f"{action} must drop the hidden source from the topic page"
        assert source_b in after, "the surviving source must still support the topic page"
