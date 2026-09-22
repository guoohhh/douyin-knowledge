"""API-layer tests.

These drive the product the way the frontend will: sync over HTTP, drain the queue with
the real worker, then read collections, sources, wiki and answers back out. Nothing is
inserted by hand, so a route that queries a column the pipeline never populates fails
here instead of in the browser.

The whole file runs with `ai_provider=mock`, which is also the state a new user is in
before configuring a key -- demo mode is a supported mode, not a test-only shortcut.
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
    # `serve_frontend=False` on purpose: these tests exercise the headless API, and
    # `frontend/dist/` is gitignored, so leaving the default on would make `/` return the
    # HTML shell on a developer machine that has run `npm run build` and JSON on CI --
    # a test whose result depends on an untracked directory. The static mount has its own
    # tests below, against a fabricated dist.
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
    """The settings dependency is overridden rather than monkeypatched into the env.

    `get_settings` is lru_cached, so a test that only sets DK_* vars would silently get
    whichever Settings instance happened to be built first in the session.
    """
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


@pytest.fixture
def worker(settings: Settings) -> Worker:
    return Worker(register_default_handlers(), settings=settings, name="test-worker")


@pytest.fixture
def populated(client: TestClient, worker: Worker) -> TestClient:
    """A synced, processed, indexed corpus built entirely through the API and queue."""
    response = client.post("/api/sources/sync", json={"auto_process": True})
    assert response.status_code == 202, response.text
    drained = worker.drain(max_jobs=500)
    assert drained > 0, "sync must produce work"
    return client


class TestHealth:
    def test_health_reports_demo_mode(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["database"] == "connected"
        assert body["demo_mode"] is True

    def test_root_advertises_prefix(self, client: TestClient) -> None:
        assert client.get("/").json()["api_prefix"] == "/api"

    def test_openapi_builds(self, client: TestClient) -> None:
        """A schema that cannot be generated means a broken response annotation."""
        assert client.get("/openapi.json").status_code == 200


class TestSyncAndProcess:
    def test_sync_enqueues_and_dedupes(self, client: TestClient) -> None:
        first = client.post("/api/sources/sync", json={}).json()
        second = client.post("/api/sources/sync", json={}).json()
        assert first["created"] is True
        assert second["created"] is False, "a second sync must reuse the pending job"
        assert first["job_id"] == second["job_id"]

    def test_sync_populates_collections(self, populated: TestClient) -> None:
        collections = populated.get("/api/sources/collections").json()["collections"]
        assert collections
        assert all(c["source_count"] > 0 for c in collections)

    def test_sources_list_is_processed(self, populated: TestClient) -> None:
        sources = populated.get("/api/sources").json()["sources"]
        assert sources
        assert any(s["processing"]["status"] == "processed" for s in sources)

    def test_source_detail_carries_the_spine(self, populated: TestClient) -> None:
        sources = populated.get("/api/sources", params={"processing_status": "processed"}).json()[
            "sources"
        ]
        detail = populated.get(f"/api/sources/{sources[0]['id']}").json()
        assert detail["evidence"], "a processed source must expose its evidence"
        assert detail["chunks"]
        for claim in detail["claims"]:
            assert claim["source_id"] == detail["source"]["id"]

    def test_unknown_source_is_404(self, client: TestClient) -> None:
        assert client.get("/api/sources/src_nope").status_code == 404

    def test_process_rejects_level_above_configured_max(self, populated: TestClient) -> None:
        source_id = populated.get("/api/sources").json()["sources"][0]["id"]
        response = populated.post(
            f"/api/sources/{source_id}/process", json={"target_level": 4}
        )
        assert response.status_code == 422, "level 4 needs OCR the demo config does not allow"

    def test_reprocess_supersedes_rather_than_duplicates(
        self, populated: TestClient, worker: Worker
    ) -> None:
        source_id = populated.get(
            "/api/sources", params={"processing_status": "processed"}
        ).json()["sources"][0]["id"]
        before = populated.get(f"/api/sources/{source_id}").json()

        assert (
            populated.post(
                f"/api/sources/{source_id}/process", json={"force": True}
            ).status_code
            == 202
        )
        worker.drain(max_jobs=500)

        after = populated.get(f"/api/sources/{source_id}").json()
        assert after["processing"]["current_processing_run_id"] != before["processing"][
            "current_processing_run_id"
        ]
        assert len(after["chunks"]) == len(before["chunks"]), (
            "the detail view reads through the currency pointer, so a second run must "
            "replace the visible chunks rather than add to them (DB-004)"
        )

    def test_delete_is_a_tombstone(self, populated: TestClient) -> None:
        source_id = populated.get("/api/sources").json()["sources"][0]["id"]
        assert populated.delete(f"/api/sources/{source_id}").status_code == 200

        remaining = [s["id"] for s in populated.get("/api/sources").json()["sources"]]
        assert source_id not in remaining

        stats = populated.get("/api/admin/stats").json()
        assert stats["corpus"]["locally_deleted"] >= 1, (
            "the row must survive as a tombstone or the next sync would resurrect it "
            "(PRIV-003)"
        )


class TestKnowledge:
    def test_entities_have_live_claim_counts(self, populated: TestClient) -> None:
        entities = populated.get("/api/knowledge/entities").json()["entities"]
        assert entities
        assert any(e["claim_count"] > 0 for e in entities)

    def test_entity_detail_attributes_every_claim(self, populated: TestClient) -> None:
        entities = populated.get("/api/knowledge/entities").json()["entities"]
        target = next(e for e in entities if e["claim_count"] > 0)
        detail = populated.get(f"/api/knowledge/entities/{target['id']}").json()
        assert detail["claims"]
        for claim in detail["claims"]:
            assert claim["source_id"], "a claim is never a global fact (KM-003)"

    def test_wiki_pages_are_supported(self, populated: TestClient) -> None:
        pages = populated.get("/api/knowledge/wiki").json()["pages"]
        assert pages, "processing should have produced wiki pages"
        detail = populated.get(f"/api/knowledge/wiki/{pages[0]['id']}").json()
        assert detail["revision"]["revision_no"] >= 1
        assert detail["supports"], "an uncited page is a bug, not a display case (WIKI-002)"

    def test_wiki_page_resolves_by_slug(self, populated: TestClient) -> None:
        page = populated.get("/api/knowledge/wiki").json()["pages"][0]
        by_slug = populated.get(f"/api/knowledge/wiki/{page['slug']}").json()
        assert by_slug["id"] == page["id"]

    def test_revisions_are_retained(self, populated: TestClient) -> None:
        page = populated.get("/api/knowledge/wiki").json()["pages"][0]
        revisions = populated.get(f"/api/knowledge/wiki/{page['id']}/revisions").json()
        assert revisions["revisions"], "history is the audit trail (WIKI-009)"

    def test_wiki_rebuild_is_idempotent(self, populated: TestClient) -> None:
        before = populated.get("/api/knowledge/wiki").json()["pages"]
        result = populated.post("/api/knowledge/wiki/rebuild").json()
        assert result["status"] == "succeeded", result
        after = populated.get("/api/knowledge/wiki").json()["pages"]
        assert len(after) == len(before)

    def test_search_returns_cited_hits(self, populated: TestClient) -> None:
        body = populated.get("/api/knowledge/search", params={"q": "香港"}).json()
        assert body["results"], body["diagnostics"]
        assert body["is_empty"] is False
        for chunk in body["results"]:
            assert chunk["source_id"], "a hit with no source cannot be cited"

    def test_search_reports_why_it_is_empty(self, populated: TestClient) -> None:
        """Empty and unprocessed are different problems and the payload must say which."""
        body = populated.get(
            "/api/knowledge/search", params={"q": "量子色动力学费曼图"}
        ).json()
        assert body["diagnostics"]


class TestConversations:
    def test_scope_preview_separates_personal_from_general(self, client: TestClient) -> None:
        personal = client.get(
            "/api/conversations/scope-preview", params={"q": "我收藏的香港餐厅"}
        ).json()
        general = client.get(
            "/api/conversations/scope-preview", params={"q": "一般来说光合作用是什么"}
        ).json()
        assert personal["scope"] == "personal_required"
        assert general["scope"] == "general"
        assert personal["matched_marker"], "the UI has to be able to explain the boundary"

    def test_unmarked_question_defaults_to_personal(self, client: TestClient) -> None:
        """RET-003: inside a personal knowledge tool an ambiguous question is personal.

        Asserted at the API boundary because this default is the product's posture, and a
        drift to `general` would quietly turn the tool into a chatbot.
        """
        body = client.get(
            "/api/conversations/scope-preview", params={"q": "什么是光合作用"}
        ).json()
        assert body["scope"] == "personal_first"

    def test_ask_produces_a_cited_answer(self, populated: TestClient) -> None:
        """DEC-006: the answer must be traceable to whatever produced it.

        This asserted `meta["model_name"]` is set, which passed only because demo mode was
        handing the generator a `MockChatModel` that echoed the canned string "Mock
        response" -- so the recorded provenance was a model that had written nothing of
        substance. Demo mode now composes the answer itself, and a deterministic answer has
        no model name to record: `generator` is the provenance, and inventing a model name
        for prose that no model wrote would be false provenance, not better provenance.

        So the assertion is on the pair: `generator` always present, `model_name` set
        exactly when a model actually ran.
        """
        body = populated.post(
            "/api/conversations/ask", json={"query": "我收藏里有哪些香港餐厅"}
        ).json()
        assert body["content"]
        assert body["has_evidence"] is True
        assert body["citations"], "a personal-scope answer must cite the collection"
        assert body["conversation_id"]

        meta = body["meta"]
        assert meta["generator"] in ("deterministic", "model")
        if meta["generator"] == "model":
            assert meta["model_name"], "a model-written answer records which model wrote it"
        else:
            assert meta["model_name"] is None, (
                "a deterministic answer must not claim a model wrote it"
            )
        # The regression this file previously accepted: placeholder text under real citations.
        from douyin_knowledge.ai.adapters.mock_adapter import MockChatModel

        assert MockChatModel().canned_response not in body["content"]
        # Every marker in the prose must resolve to a returned citation.
        import re

        markers = {int(m) for m in re.findall(r"\[(\d+)\]", body["content"])}
        assert markers <= {c["ordinal"] for c in body["citations"]}

    def test_turns_accumulate_in_one_conversation(self, populated: TestClient) -> None:
        conversation_id = populated.post(
            "/api/conversations", json={"title": "香港"}
        ).json()["id"]
        for query in ("我收藏里有哪些香港餐厅", "哪家最便宜"):
            response = populated.post(
                f"/api/conversations/{conversation_id}/messages", json={"query": query}
            )
            assert response.status_code == 200, response.text

        detail = populated.get(f"/api/conversations/{conversation_id}").json()
        assert len(detail["messages"]) == 4, "two turns is two user plus two assistant rows"

    def test_citations_endpoint_resolves_message_evidence(self, populated: TestClient) -> None:
        turn = populated.post(
            "/api/conversations/ask", json={"query": "我收藏里有哪些香港餐厅"}
        ).json()
        citations = populated.get(
            f"/api/conversations/{turn['conversation_id']}"
            f"/messages/{turn['assistant_message_id']}/citations"
        ).json()
        assert citations["citations"]

    def test_general_scope_answer_has_no_collection_citations(
        self, populated: TestClient
    ) -> None:
        body = populated.post(
            "/api/conversations/ask",
            json={"query": "什么是光合作用", "scope_override": "general"},
        ).json()
        assert body["citations"] == [], (
            "general knowledge must not borrow the collection's authority (RET-002)"
        )

    def test_conversation_delete(self, client: TestClient) -> None:
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        assert client.delete(f"/api/conversations/{conversation_id}").status_code == 200
        assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


class TestAdmin:
    def test_stats_count_the_spine(self, populated: TestClient) -> None:
        stats = populated.get("/api/admin/stats").json()
        assert stats["corpus"]["sources"] > 0
        assert stats["corpus"]["processed_sources"] > 0
        assert stats["knowledge"]["claims"] > 0
        assert stats["index"]["search_documents"] > 0

    def test_settings_never_leak_secrets(self, client: TestClient) -> None:
        body = client.get("/api/admin/settings").json()
        assert body["openai_api_key"] is False
        assert isinstance(body["resolved_models"]["extraction"], str)
        assert "sk-" not in client.get("/api/admin/settings").text

    def test_jobs_are_listable_and_inspectable(self, populated: TestClient) -> None:
        jobs = populated.get("/api/admin/jobs").json()["jobs"]
        assert jobs
        detail = populated.get(f"/api/admin/jobs/{jobs[0]['id']}").json()
        assert detail["events"], "a job that ran must have left an event trail"

    def test_cancel_a_queued_job(self, client: TestClient) -> None:
        job_id = client.post("/api/sources/sync", json={}).json()["job_id"]
        body = client.post(f"/api/admin/jobs/{job_id}/cancel").json()
        assert body["cancelled"] is True
        assert body["status"] == "cancelled"

    def test_processing_status_lists_runs(self, populated: TestClient) -> None:
        body = populated.get("/api/admin/processing/status").json()
        assert body["runs"]
        assert body["failed_sources"] == []
        assert all(r["models"] for r in body["runs"]), "every run records its provenance"

    def test_reindex_is_queued_not_inline(self, populated: TestClient, worker: Worker) -> None:
        response = populated.post("/api/admin/reindex", json={"scope": "vectors"})
        assert response.status_code == 202
        assert worker.drain(max_jobs=50) >= 1

    def test_policy_rule_round_trip(self, client: TestClient) -> None:
        created = client.post(
            "/api/admin/policy/rules",
            json={
                "name": "skip dance videos",
                "rule_type": "metadata",
                "action": "exclude",
                "priority": 10,
                "matcher": {"title_contains": "舞蹈"},
            },
        )
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]
        assert rule_id, "the repository must assign an id rather than storing NULL"

        rules = client.get("/api/admin/policy/rules").json()["rules"]
        assert any(r["id"] == rule_id for r in rules)

        assert client.delete(f"/api/admin/policy/rules/{rule_id}").status_code == 200
        assert client.delete(f"/api/admin/policy/rules/{rule_id}").status_code == 404

    def test_a_semantic_rule_needs_real_content_types(self, client: TestClient) -> None:
        """A typo here would fail silently and expensively (DEC-016).

        The rule would be stored, listed and shown as enabled while matching nothing, so
        the user believes they are skipping variety clips and pays to process every one.
        """
        base = {"rule_type": "semantic", "action": "exclude"}

        typo = client.post(
            "/api/admin/policy/rules", json={**base, "matcher": {"content_types": ["varity_clip"]}}
        )
        assert typo.status_code == 422, typo.text

        empty = client.post("/api/admin/policy/rules", json={**base, "matcher": {}})
        assert empty.status_code == 422

        # `unknown` is rejected too: it means triage could not tell, and letting it drive
        # an exclusion would turn every classifier miss into silent data loss.
        unknown = client.post(
            "/api/admin/policy/rules", json={**base, "matcher": {"content_types": ["unknown"]}}
        )
        assert unknown.status_code == 422

        ok = client.post(
            "/api/admin/policy/rules",
            json={**base, "matcher": {"content_types": ["variety_clip"], "min_confidence": 0.8}},
        )
        assert ok.status_code == 201, ok.text

    def test_a_metadata_rule_needs_at_least_one_condition(self, client: TestClient) -> None:
        """Same silent failure as DEC-016, on the other rule type.

        `PolicyEvaluator._matcher_matches` returns False for an empty matcher so a
        half-saved rule cannot switch off the pipeline. Without this check the half-saved
        rule is still accepted, listed and shown as enabled while matching nothing.
        """
        base = {"rule_type": "metadata", "action": "exclude"}

        assert client.post("/api/admin/policy/rules", json={**base, "matcher": {}}).status_code == 422
        assert client.post("/api/admin/policy/rules", json=base).status_code == 422
        # Present but empty is still no condition.
        blank = client.post(
            "/api/admin/policy/rules", json={**base, "matcher": {"keywords": []}}
        )
        assert blank.status_code == 422
        # A key the evaluator never reads cannot stand in for a real condition.
        unread = client.post(
            "/api/admin/policy/rules", json={**base, "matcher": {"titel_contains": ["广告"]}}
        )
        assert unread.status_code == 422, unread.text

    def test_the_rule_shapes_the_settings_ui_builds_are_accepted(
        self, client: TestClient
    ) -> None:
        """The Settings form generates matchers for the user; these are the shapes it sends.

        The UI deliberately hides matcher JSON, so nothing in the frontend would tell us if
        the key names drifted from what the evaluator reads -- the rule would just quietly
        never match. This pins the contract from the backend side.
        """
        semantic = client.post(
            "/api/admin/policy/rules",
            json={
                "name": "跳过影视剪辑",
                "rule_type": "semantic",
                "action": "exclude",
                "priority": 50,
                "is_enabled": True,
                "matcher": {"content_types": ["movie_clip", "variety_clip", "meme"]},
            },
        )
        assert semantic.status_code == 201, semantic.text
        assert semantic.json()["matcher"]["content_types"] == [
            "movie_clip",
            "variety_clip",
            "meme",
        ]

        metadata = client.post(
            "/api/admin/policy/rules",
            json={
                "name": "跳过广告",
                "rule_type": "metadata",
                "action": "metadata_only",
                "priority": 50,
                "is_enabled": True,
                "matcher": {"keywords": ["开箱", "好物推荐"], "hashtags": ["广告"]},
            },
        )
        assert metadata.status_code == 201, metadata.text
        saved = metadata.json()["matcher"]
        assert saved["keywords"] == ["开箱", "好物推荐"]
        assert saved["hashtags"] == ["广告"]

        # Every content type the form offers must be accepted, or a checkbox is a dead end.
        for label in (
            "movie_clip",
            "variety_clip",
            "music_clip",
            "meme",
            "sports_highlight",
            "other_entertainment",
        ):
            response = client.post(
                "/api/admin/policy/rules",
                json={
                    "rule_type": "semantic",
                    "action": "exclude",
                    "matcher": {"content_types": [label]},
                },
            )
            assert response.status_code == 201, f"{label}: {response.text}"

    def test_sources_expose_their_triage_label(self, populated: TestClient) -> None:
        """The label has to be visible, or a user cannot tell why a rule would apply.

        Unclassified reads as `null` rather than `"unknown"`: the demo corpus has no
        semantic rules, so nothing has been classified, and that is a different statement
        from "classified and inconclusive" (DEC-016).
        """
        listing = populated.get("/api/sources", params={"limit": 5}).json()["sources"]
        assert listing
        assert all(s["triage"]["content_type"] is None for s in listing)

        detail = populated.get(f"/api/sources/{listing[0]['id']}").json()
        assert detail["triage"]["content_type"] is None

        # Filtering on a label nothing carries returns nothing rather than everything.
        filtered = populated.get("/api/sources", params={"content_type": "movie_clip"}).json()
        assert filtered["total"] == 0
        assert filtered["sources"] == []

    def test_decisions_are_recorded_during_processing(self, populated: TestClient) -> None:
        decisions = populated.get("/api/admin/policy/decisions").json()["decisions"]
        assert decisions, "every processed source should carry an explainable decision"
        assert all(d["reason_code"] for d in decisions)
        assert all(d["explanation"] for d in decisions), (
            "the decision must answer 'why' without anyone reading the rule table"
        )

    def test_an_exclude_rule_actually_blocks_processing(
        self, client: TestClient, worker: Worker
    ) -> None:
        """The policy layer has to gate real work, not just store rows.

        Asserted through the whole stack because the evaluator was previously wired to
        nothing at all: it computed decisions no caller ever asked for, so the promise
        that a user can stop a creator from being processed was not kept anywhere.
        """
        client.post("/api/sources/sync", json={"auto_process": False})
        worker.drain(max_jobs=500)

        source = client.get("/api/sources").json()["sources"][0]
        creator_id = source["creator"]["id"]
        created = client.post(
            "/api/admin/policy/rules",
            json={
                "name": "skip this creator",
                "rule_type": "creator",
                "action": "exclude",
                "priority": 100,
                "target_creator_id": creator_id,
            },
        )
        assert created.status_code == 201, created.text

        assert (
            client.post(f"/api/sources/{source['id']}/process", json={}).status_code == 202
        )
        assert worker.drain(max_jobs=50) >= 1, "the job must run and decide, not sit queued"

        # Control: an unruled source from the same sync still processes, so the assertion
        # below is about the rule rather than about nothing having happened yet.
        other = next(
            s
            for s in client.get("/api/sources").json()["sources"]
            if s["creator"] and s["creator"]["id"] != creator_id
        )
        client.post(f"/api/sources/{other['id']}/process", json={})
        worker.drain(max_jobs=50)
        assert (
            client.get(f"/api/sources/{other['id']}").json()["processing"][
                "current_processing_run_id"
            ]
            is not None
        )

        detail = client.get(f"/api/sources/{source['id']}").json()
        assert detail["processing"]["current_processing_run_id"] is None, (
            "an excluded source must not get a processing run"
        )

        decisions = client.get(
            "/api/admin/policy/decisions", params={"source_id": source["id"]}
        ).json()["decisions"]
        assert decisions[0]["action"] == "exclude"
        assert decisions[0]["rule_id"] == created.json()["id"]


class TestAskContract:
    """The ask payload is a contract with the frontend, and it broke silently once.

    The client sent `scope`; the model declares `scope_override`. Pydantic's default is to
    ignore unknown keys, so the request succeeded, the override was dropped, and the answer
    crossed the knowledge boundary the caller had explicitly restricted -- visible nowhere
    except in an answer that looked fine. These tests pin the failure loud.
    """

    def test_unknown_field_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/conversations/ask", json={"query": "x", "scope": "general"}
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "scope"]

    def test_scope_override_is_honoured(self, populated: TestClient) -> None:
        body = client_ask(populated, {"query": "珠穆朗玛峰有多高", "scope_override": "general"})
        assert body["scope"] == "general"

    def test_ask_can_continue_a_conversation(self, populated: TestClient) -> None:
        """Follow-up resolution depends on the turn landing in the same conversation.

        `/ask` accepted `conversation_id` from the client and ignored it, so every follow-up
        started a fresh thread and "第二家呢" had nothing to resolve against (DEC-004).
        """
        first = client_ask(populated, {"query": "我收藏里有哪些香港餐厅"})
        second = client_ask(
            populated, {"query": "人均多少", "conversation_id": first["conversation_id"]}
        )
        assert second["conversation_id"] == first["conversation_id"]

        messages = populated.get(f"/api/conversations/{first['conversation_id']}").json()[
            "messages"
        ]
        assert len(messages) == 4, "two turns means two user and two assistant messages"

    def test_unknown_conversation_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/conversations/ask", json={"query": "x", "conversation_id": "cnv_missing"}
        )
        assert response.status_code == 404


def client_ask(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/conversations/ask", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


class TestFrontendMount:
    """`DK_SERVE_FRONTEND` was documented in settings and in .env.example while nothing
    read it, so the single-process deployment the docs describe did not exist.

    The dist directory is fabricated here rather than depending on `npm run build`: the
    behaviour under test is the routing, and a test that needs a node toolchain present
    would just be skipped on the machine where it matters.
    """

    @pytest.fixture
    def served(self, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch):
        from douyin_knowledge import api as api_module

        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>shell</title>", "utf-8")
        (dist / "assets" / "app.js").write_text("export default 1\n", "utf-8")
        monkeypatch.setattr(api_module, "_frontend_dist", lambda: dist)

        settings.serve_frontend = True
        app = create_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as c:
            yield c

    def test_root_serves_the_shell(self, served: TestClient) -> None:
        response = served.get("/")
        assert response.status_code == 200
        assert "shell" in response.text

    def test_client_route_deep_links(self, served: TestClient) -> None:
        """A bookmarked /wiki/wp_x must load the app, not 404: only the bundle can route it."""
        response = served.get("/wiki/wp_abc")
        assert response.status_code == 200
        assert "shell" in response.text

    def test_asset_is_served_as_a_file(self, served: TestClient) -> None:
        assert served.get("/assets/app.js").text.strip() == "export default 1"

    def test_missing_asset_stays_a_404(self, served: TestClient) -> None:
        """Returning the shell for a missing script would surface as a syntax error in the
        browser instead of the missing file it actually is."""
        assert served.get("/assets/nope.js").status_code == 404

    def test_unknown_api_path_stays_json(self, served: TestClient) -> None:
        """An unmatched API route must not fall through to HTML; the client parses JSON."""
        response = served.get("/api/bogus")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")

    def test_api_still_answers_under_the_mount(self, served: TestClient) -> None:
        assert served.get("/health").json()["status"] == "ok"

    def test_disabled_setting_leaves_the_banner(self, client: TestClient) -> None:
        assert client.get("/").json()["api_prefix"] == "/api"


class TestPolicyReconciliation:
    """Rule changes must reach sources that were already processed (P0-3, DEC-015).

    These run against `populated`, a corpus synced, processed and indexed through the real
    queue, because the behaviour under test is precisely what happens to *existing*
    knowledge when policy changes -- a fresh database cannot exhibit it.
    """

    def _processed_source_id(self, client: TestClient) -> str:
        sources = client.get(
            "/api/sources", params={"processing_status": "processed"}
        ).json()["sources"]
        assert sources, "the fixture must have processed something"
        return sources[0]["id"]

    def test_creating_an_exclude_rule_hides_the_source(self, populated: TestClient) -> None:
        source_id = self._processed_source_id(populated)

        response = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        )

        assert response.status_code == 201, response.text
        assert response.json()["reconciled"]["now_hidden"] == 1

    def test_deleting_the_rule_reverses_it(self, populated: TestClient) -> None:
        source_id = self._processed_source_id(populated)
        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        ).json()

        response = populated.delete(f"/api/admin/policy/rules/{created['id']}")

        assert response.status_code == 200
        assert response.json()["reconciled"]["now_visible"] == 1

    def test_disabling_the_rule_reverses_it(self, populated: TestClient) -> None:
        """Disable is the reversible form of delete and must behave identically."""
        source_id = self._processed_source_id(populated)
        created = populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        ).json()

        response = populated.patch(
            f"/api/admin/policy/rules/{created['id']}", params={"enabled": False}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_enabled"] is False
        assert body["reconciled"]["now_visible"] == 1

    def test_excluded_source_stops_being_cited(self, populated: TestClient) -> None:
        """The user-visible payoff: the answer stops citing what was excluded."""
        before = populated.post("/api/conversations/ask", json={"query": "有哪些茶餐厅"})
        assert before.status_code == 200, before.text
        cited_before = {c["source_id"] for c in before.json().get("citations", [])}
        assert cited_before, "the fixture must cite something for this test to mean anything"
        # Exclude a source the answer actually cited, not merely one that was processed.
        source_id = sorted(cited_before)[0]

        populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        )

        after = populated.post("/api/conversations/ask", json={"query": "有哪些茶餐厅"})
        cited_after = {c["source_id"] for c in after.json().get("citations", [])}
        assert source_id not in cited_after

    def test_history_survives_exclusion(self, populated: TestClient) -> None:
        """Exclusion is a visibility change: the source and its evidence are still there."""
        source_id = self._processed_source_id(populated)
        populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        )

        detail = populated.get(f"/api/sources/{source_id}").json()

        assert detail["evidence"], "evidence must not be deleted by a policy change"
        assert detail["id"] == source_id
        assert detail["processing"]["status"] == "processed", "the run pointer survives"

    def test_detail_explains_why_a_source_is_hidden(self, populated: TestClient) -> None:
        """A processed-but-absent source is indistinguishable from a bug unless it says why."""
        source_id = self._processed_source_id(populated)
        before = populated.get(f"/api/sources/{source_id}").json()
        assert before["processing"]["excluded"] is False
        assert before["processing"]["policy_action"] == "process"

        rule = populated.post(
            "/api/admin/policy/rules",
            json={
                "rule_type": "source",
                "action": "exclude",
                "target_source_id": source_id,
                "name": "不看这条",
            },
        ).json()

        detail = populated.get(f"/api/sources/{source_id}").json()

        assert detail["processing"]["excluded"] is True
        assert detail["processing"]["policy_action"] == "exclude"
        assert detail["policy"]["action"] == "exclude"
        assert detail["policy"]["rule_id"] == rule["id"]
        assert detail["policy"]["rule_name"] == "不看这条"
        assert detail["policy"]["decision_id"], "the decision pointer must resolve"

    def test_list_marks_excluded_sources(self, populated: TestClient) -> None:
        source_id = self._processed_source_id(populated)
        populated.post(
            "/api/admin/policy/rules",
            json={"rule_type": "source", "action": "exclude", "target_source_id": source_id},
        )

        items = populated.get("/api/sources", params={"limit": 100}).json()["sources"]
        by_id = {item["id"]: item for item in items}

        assert by_id[source_id]["processing"]["excluded"] is True
        others = [i for sid, i in by_id.items() if sid != source_id]
        assert others, "the fixture must hold more than one source"
        assert all(i["processing"]["excluded"] is False for i in others)

    def test_reconcile_endpoint_rebuilds_derived_state(self, populated: TestClient) -> None:
        response = populated.post("/api/admin/policy/reconcile")

        assert response.status_code == 200
        assert "reevaluated" in response.json()

    def test_deleting_an_unknown_rule_is_404(self, client: TestClient) -> None:
        assert client.delete("/api/admin/policy/rules/rule_nope").status_code == 404
