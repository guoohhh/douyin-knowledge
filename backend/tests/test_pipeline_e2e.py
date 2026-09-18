"""End-to-end pipeline test: capture -> process -> index -> retrieve.

This test exists because the previous suite constructed model rows by hand and
never called the extractors, so four modules could drift out of sync with the
schema while the suite stayed green. Everything here goes through the real code
paths against the real schema, on the demo fixtures.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from douyin_knowledge.ai.adapters.mock_adapter import MockEmbeddingModel, MockStructuredModel
from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider
from douyin_knowledge.capture.sync import CaptureSyncService, SyncStats
from douyin_knowledge.config.settings import Settings
from douyin_knowledge.conversation.answer_generator import NO_EVIDENCE_TEMPLATE
from douyin_knowledge.conversation.conversation_manager import ConversationManager
from douyin_knowledge.conversation.scope import classify_scope
from douyin_knowledge.db.base import Base
from douyin_knowledge.db.models.capture import Source
from douyin_knowledge.db.models.conversation import ConversationState, MessageCitation
from douyin_knowledge.db.models.entities import Claim, ClaimEvidence, Entity, EntityMention
from douyin_knowledge.db.models.policy import SourceProcessingState
from douyin_knowledge.db.models.processing import EvidenceUnit, ProcessingRun, RetrievalChunk
from douyin_knowledge.db.models.search import SearchDocument
from douyin_knowledge.db.models.wiki import WikiPage, WikiRevision, WikiSupport
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator
from douyin_knowledge.retrieval.retriever import HybridRetriever
from douyin_knowledge.retrieval.vector_store import VectorStore
from douyin_knowledge.search import indexer
from douyin_knowledge.wiki.builder import WikiBuilder
from douyin_knowledge.wiki.updater import WikiUpdater


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", ai_provider="mock")


@pytest.fixture
def session_factory(tmp_path):
    """Real file-backed SQLite so FTS5 and its triggers are exercised."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    _create_fts(engine)
    return sessionmaker(bind=engine, future=True)


def _create_fts(engine) -> None:
    """Mirror migration 0002 so the test hits the same FTS setup as production."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE search_fts USING fts5(
                    title, body, content='search_documents', content_rowid='rowid',
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER search_documents_ai AFTER INSERT ON search_documents BEGIN
                    INSERT INTO search_fts(rowid, title, body)
                    VALUES (new.rowid, new.title, new.body);
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER search_documents_ad AFTER DELETE ON search_documents BEGIN
                    INSERT INTO search_fts(search_fts, rowid, title, body)
                    VALUES ('delete', old.rowid, old.title, old.body);
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER search_documents_au AFTER UPDATE ON search_documents BEGIN
                    INSERT INTO search_fts(search_fts, rowid, title, body)
                    VALUES ('delete', old.rowid, old.title, old.body);
                    INSERT INTO search_fts(rowid, title, body)
                    VALUES (new.rowid, new.title, new.body);
                END
                """
            )
        )


@pytest.fixture
def synced(session_factory, settings) -> tuple[sessionmaker, list[str]]:
    provider = FixtureCaptureProvider()
    with session_factory() as session:
        service = CaptureSyncService(session, platform="douyin")
        stats = None
        for captured_collection in provider.list_collections():
            stats = service.sync_collection(provider, captured_collection, stats=stats)
        session.commit()
        source_ids = list(stats.source_ids) if stats else []
    return session_factory, source_ids


def _orchestrator(settings: Settings) -> ProcessingOrchestrator:
    return ProcessingOrchestrator(settings, structured_model=MockStructuredModel())


class TestCaptureSync:
    def test_sync_creates_sources_and_is_idempotent(self, session_factory, settings):
        provider = FixtureCaptureProvider()
        collections = provider.list_collections()

        def run_sync(factory) -> SyncStats:
            with factory() as session:
                service = CaptureSyncService(session, platform="douyin")
                stats = None
                for captured in collections:
                    stats = service.sync_collection(provider, captured, stats=stats)
                session.commit()
                return stats

        first = run_sync(session_factory)
        assert first.sources_created > 0
        assert first.snapshots == first.sources_created

        second = run_sync(session_factory)
        assert second.sources_created == 0, "re-syncing unchanged data must not create sources"
        assert second.sources_unchanged > 0
        assert second.snapshots == 0, "identical payloads must not accumulate snapshots"

    def test_native_subtitle_becomes_evidence(self, synced):
        factory, _ = synced
        with factory() as session:
            subtitles = session.scalars(
                select(EvidenceUnit).where(EvidenceUnit.kind == "subtitle")
            ).all()
        assert subtitles, "fixtures carry inline subtitles; they must land as evidence"
        assert all(unit.confidence == 1.0 for unit in subtitles)


class TestProcessing:
    def test_process_source_produces_full_spine(self, synced, settings):
        factory, source_ids = synced
        with factory() as session:
            source = session.scalars(
                select(Source).where(Source.external_id == "v_hku_food_list")
            ).one()
            outcome = _orchestrator(settings).process_source(session, source.id, target_level=2)
            session.commit()

            assert outcome.succeeded
            assert outcome.achieved_level == 2, "inline subtitle should reach level 2"
            assert outcome.evidence_count > 0
            assert outcome.mention_count > 0, "the food list names five restaurants"
            assert outcome.claim_count > 0, "it states five prices"
            assert outcome.chunk_count > 0

            # Every claim must be citable through the provenance spine.
            claims = session.scalars(
                select(Claim).where(Claim.processing_run_id == outcome.run_id)
            ).all()
            for claim in claims:
                links = session.scalars(
                    select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)
                ).all()
                assert links, f"claim {claim.id} has no evidence"
                assert claim.attribution, "attribution is NOT NULL and must be meaningful"

    def test_price_claim_is_numeric_and_comparable(self, synced, settings):
        factory, _ = synced
        with factory() as session:
            source = session.scalars(
                select(Source).where(Source.external_id == "v_hku_food_list")
            ).one()
            _orchestrator(settings).process_source(session, source.id, target_level=2)
            session.commit()

            prices = session.scalars(
                select(Claim).where(
                    Claim.source_id == source.id, Claim.predicate == "price_per_person"
                )
            ).all()

        assert prices, "spoken Chinese prices must become claims"
        assert any(c.value_number == 80.0 for c in prices), "人均八十 -> 80.0"
        assert all(c.value_type == "number" for c in prices)
        assert all(c.currency for c in prices)

    def test_currency_pointer_advances_only_on_success(self, synced, settings):
        factory, _ = synced
        with factory() as session:
            source = session.scalars(
                select(Source).where(Source.external_id == "v_hku_food_list")
            ).one()
            first = _orchestrator(settings).process_source(session, source.id, target_level=2)
            session.commit()

            state = session.get(SourceProcessingState, source.id)
            assert state is not None
            assert state.current_processing_run_id == first.run_id

            # Reprocess: a new run must supersede the old one, not mutate it.
            second = _orchestrator(settings).process_source(session, source.id, target_level=2)
            session.commit()

            state = session.get(SourceProcessingState, source.id)
            assert state.current_processing_run_id == second.run_id
            assert first.run_id != second.run_id

            # The old run's rows still exist; they are simply no longer current.
            old_run = session.get(ProcessingRun, first.run_id)
            assert old_run is not None
            assert old_run.status == "succeeded"

    def test_reprocessing_hides_superseded_chunks_from_retrieval(self, synced, settings):
        factory, _ = synced
        with factory() as session:
            source = session.scalars(
                select(Source).where(Source.external_id == "v_hku_food_list")
            ).one()
            orch = _orchestrator(settings)
            first = orch.process_source(session, source.id, target_level=2)
            second = orch.process_source(session, source.id, target_level=2)
            session.commit()

            retriever = HybridRetriever(session)
            allowed = retriever._current_chunk_ids([source.id])

            old_chunks = session.scalars(
                select(RetrievalChunk.id).where(
                    RetrievalChunk.processing_run_id == first.run_id
                )
            ).all()
            new_chunks = session.scalars(
                select(RetrievalChunk.id).where(
                    RetrievalChunk.processing_run_id == second.run_id
                )
            ).all()

        assert new_chunks
        assert allowed == set(new_chunks), "only the current run may be retrievable (DB-004)"
        assert not (allowed & set(old_chunks)), "superseded chunks must be unreachable"


class TestEntityResolution:
    def test_repeated_name_resolves_to_one_entity(self, synced, settings):
        """好运茶餐厅 appears in two fixture videos; it must be one entity."""
        factory, _ = synced
        with factory() as session:
            orch = _orchestrator(settings)
            for external_id in ("v_hku_food_list", "v_hoyun_revisit"):
                source = session.scalars(
                    select(Source).where(Source.external_id == external_id)
                ).one()
                orch.process_source(session, source.id, target_level=2)
            session.commit()

            entities = session.scalars(
                select(Entity).where(Entity.canonical_name.like("%好运%"))
            ).all()
            names = [e.canonical_name for e in entities]

        assert len(entities) == 1, f"expected one 好运 entity, got {names}"

    def test_fuzzy_match_never_auto_merges(self, synced, settings):
        """ENT-002: only exact normalized identity may merge automatically."""
        factory, _ = synced
        with factory() as session:
            from douyin_knowledge.extraction.entity_resolver import (
                STATUS_AMBIGUOUS,
                EntityResolver,
            )

            source = session.scalars(select(Source)).first()
            run = ProcessingRun(
                source_id=source.id,
                run_kind="extract",
                processor_version="test",
                schema_version="1",
                target_level=1,
                achieved_level=1,
                status="succeeded",
            )
            session.add(run)
            session.flush()

            session.add(
                Entity(
                    entity_type="place",
                    canonical_name="好运茶餐厅",
                    normalized_name="好运茶餐厅",
                    status="active",
                )
            )
            session.flush()

            # One character different: plausibly a typo, plausibly another shop.
            mention = EntityMention(
                source_id=source.id,
                processing_run_id=run.id,
                mention_text="好运茶餐店",
                normalized_text="好运茶餐店",
                entity_type_hint="place",
                resolution_status="unresolved",
            )
            session.add(mention)
            session.flush()

            outcome = EntityResolver().resolve_mention(session, mention)

            assert outcome.status == STATUS_AMBIGUOUS
            assert mention.resolved_entity_id is None, "a fuzzy match must not merge"
            assert outcome.candidates, "but it must be offered for review"


class TestSearchAndRetrieval:
    def test_index_and_retrieve_chinese_query(self, synced, settings):
        factory, _ = synced
        with factory() as session:
            orch = _orchestrator(settings)
            for source in session.scalars(select(Source)).all():
                orch.process_source(session, source.id, target_level=2)
            session.commit()

            store = VectorStore(settings.vector_dir)
            embedder = MockEmbeddingModel()
            stats = indexer.reindex_all(
                session, store=store, embedder=embedder, model_name="mock-embedding"
            )
            session.commit()

            assert stats.inserted > 0
            assert stats.embedded > 0

            docs = session.scalars(select(SearchDocument)).all()
            assert docs

            retriever = HybridRetriever(session, vector_store=store, embedder=embedder)
            result = retriever.retrieve("人均八十的茶餐厅", limit=5)

            assert not result.is_empty(), f"diagnostics: {result.diagnostics}"
            assert result.chunks
            # Two-character CJK queries are the dominant shape and must work.
            assert all(chunk.evidence_ids for chunk in result.chunks)

    def test_two_char_query_matches(self, synced, settings):
        """The exact failure mode bare unicode61 and trigram both have."""
        factory, _ = synced
        with factory() as session:
            orch = _orchestrator(settings)
            for source in session.scalars(select(Source)).all():
                orch.process_source(session, source.id, target_level=2)
            indexer.reindex_all(session)
            session.commit()

            retriever = HybridRetriever(session)
            result = retriever.retrieve("人均", limit=5)

        assert result.chunks, "a 2-character CJK query must return results"

    def test_vector_store_persists_across_instances(self, settings):
        store = VectorStore(settings.vector_dir)
        store.upsert("chunk:abc", [0.1, 0.2, 0.3])
        store.upsert("chunk:def", [0.9, 0.1, 0.0])
        store.save()

        reopened = VectorStore(settings.vector_dir)
        assert len(reopened) == 2
        assert "chunk:abc" in reopened

        hits = reopened.search([0.1, 0.2, 0.3], limit=1)
        assert hits
        assert hits[0].object_id == "abc"
        assert hits[0].doc_type == "chunk"


class TestWiki:
    """The wiki is a compiled view; these tests pin the properties that make it
    safe to trust — every statement cited, no duplicate revisions, and a full
    rebuild reproducing the same content."""

    @pytest.fixture
    def processed(self, synced, settings):
        factory, source_ids = synced
        with factory() as session:
            orch = _orchestrator(settings)
            for source_id in source_ids:
                orch.process_source(session, source_id, target_level=2)
            session.commit()
        return factory, source_ids

    def test_integration_creates_cited_pages(self, processed, settings):
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            results = [updater.integrate_source(sid) for sid in source_ids]
            session.commit()

            assert any(r.status == "succeeded" for r in results)

            pages = session.scalars(select(WikiPage).where(WikiPage.status == "active")).all()
            assert pages, "resolved entities must produce wiki pages"

            for page in pages:
                revision = session.scalars(
                    select(WikiRevision)
                    .where(WikiRevision.page_id == page.id)
                    .where(WikiRevision.is_current.is_(True))
                ).one()
                supports = session.scalars(
                    select(WikiSupport).where(WikiSupport.wiki_revision_id == revision.id)
                ).all()
                assert supports, f"page {page.title} has no provenance"
                # Every fact statement must resolve to a real claim row.
                for support in supports:
                    if support.claim_id:
                        assert session.get(Claim, support.claim_id) is not None

    def test_statement_supports_match_statement_keys(self, processed):
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            for sid in source_ids:
                updater.integrate_source(sid)
            session.commit()

            revision = session.scalars(
                select(WikiRevision).where(WikiRevision.is_current.is_(True))
            ).first()
            supports = session.scalars(
                select(WikiSupport).where(WikiSupport.wiki_revision_id == revision.id)
            ).all()
            keys = {s.statement_key for s in supports}
            assert "summary" in keys, "the synthesized summary needs source-level provenance"
            fact_keys = {k for k in keys if k.startswith("fact:")}
            assert fact_keys, "at least one fact statement must be cited"
            # Keys are predicate-derived, so they must appear in the frontmatter.
            predicates = set(revision.frontmatter_json.get("predicates", []))
            assert {k.split(":", 1)[1] for k in fact_keys} <= predicates

    def test_reintegration_is_idempotent(self, processed):
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            for sid in source_ids:
                updater.integrate_source(sid)
            session.commit()
            first_count = len(session.scalars(select(WikiRevision.id)).all())

            for sid in source_ids:
                updater.integrate_source(sid)
            session.commit()
            second_count = len(session.scalars(select(WikiRevision.id)).all())

        assert second_count == first_count, "unchanged content must not append revisions"

    def test_rebuild_reproduces_content(self, processed):
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            for sid in source_ids:
                updater.integrate_source(sid)
            session.commit()
            before = {
                page.canonical_key: session.scalars(
                    select(WikiRevision.content_hash)
                    .where(WikiRevision.page_id == page.id)
                    .where(WikiRevision.is_current.is_(True))
                ).one()
                for page in session.scalars(select(WikiPage)).all()
            }

            updater.rebuild_all()
            session.commit()
            after = {
                page.canonical_key: session.scalars(
                    select(WikiRevision.content_hash)
                    .where(WikiRevision.page_id == page.id)
                    .where(WikiRevision.is_current.is_(True))
                ).one()
                for page in session.scalars(select(WikiPage)).all()
            }

        assert after == before, "the wiki must be reproducible from the spine"

    def test_only_one_current_revision_per_page(self, processed):
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            for sid in source_ids:
                updater.integrate_source(sid)
            session.commit()

            page = session.scalars(select(WikiPage)).first()
            # Force a content change so a second revision is appended.
            builder = WikiBuilder(session)
            entity = session.get(Entity, page.entity_id)
            entity.canonical_name = entity.canonical_name + "（分店）"
            builder.build_entity_page(entity)
            session.commit()

            current = session.scalars(
                select(WikiRevision)
                .where(WikiRevision.page_id == page.id)
                .where(WikiRevision.is_current.is_(True))
            ).all()
            assert len(current) == 1
            assert current[0].revision_no == 2
            assert current[0].mutation_type == "update_page"

    def test_ambiguous_mentions_never_reach_the_wiki(self, processed):
        """ENT-002 at the wiki boundary: a guess must not become compiled knowledge."""
        factory, source_ids = processed
        with factory() as session:
            updater = WikiUpdater(session)
            ambiguous = session.scalars(
                select(EntityMention).where(EntityMention.resolution_status == "ambiguous")
            ).all()
            for mention in ambiguous:
                assert mention.resolved_entity_id is None
                candidates = updater.select_candidate_entities(mention.source_id)
                assert mention.id not in candidates


class TestScopeClassification:
    """RET-002/RET-003 are a trust boundary, so the rules get pinned directly."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("我收藏里有哪些日料？", "personal_required"),
            ("我之前存过的餐厅推荐一下", "personal_required"),
            ("MCP 一般来说是什么？", "general"),
            ("结合我的收藏和你的知识做个学习路线", "hybrid"),
            ("香港有什么适合约会的地方？", "personal_first"),
        ],
    )
    def test_scope_rules(self, query, expected):
        assert classify_scope(query).scope == expected

    def test_hybrid_beats_personal(self):
        """A hybrid request contains personal wording; it must not be downgraded."""
        decision = classify_scope("结合我的收藏，再补充一些通用建议")
        assert decision.scope == "hybrid"
        assert decision.allows_general_knowledge

    def test_override_wins(self):
        assert classify_scope("我收藏里有什么", override="general").scope == "general"

    def test_personal_scopes_require_evidence(self):
        assert classify_scope("我收藏里有什么").requires_evidence
        assert classify_scope("香港哪里好吃").requires_evidence
        assert not classify_scope("MCP 一般来说是什么").requires_evidence


class TestConversation:
    @pytest.fixture
    def indexed(self, synced, settings):
        factory, source_ids = synced
        with factory() as session:
            orch = _orchestrator(settings)
            for source_id in source_ids:
                orch.process_source(session, source_id, target_level=2)
            session.commit()
            indexer.reindex_all(
                session,
                store=VectorStore(settings.vector_dir),
                embedder=MockEmbeddingModel(),
                model_name="mock-embedding",
            )
            session.commit()
        return factory, source_ids

    def _manager(self, session, settings):
        return ConversationManager(
            session,
            vector_store=VectorStore(settings.vector_dir),
            embedder=MockEmbeddingModel(),
        )

    def test_ask_produces_cited_answer(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            turn = manager.ask("我收藏里有哪些人均便宜的餐厅？")
            session.commit()

            assert turn.answer.has_evidence
            assert turn.citations, "a personal-scope answer must carry citations"
            # Every marker in the text must exist in the citation set.
            import re

            markers = {int(m) for m in re.findall(r"\[(\d+)\]", turn.answer.content)}
            ordinals = {c["ordinal"] for c in turn.citations}
            assert markers <= ordinals

            stored = session.scalars(
                select(MessageCitation).where(
                    MessageCitation.message_id == turn.assistant_message_id
                )
            ).all()
            assert len(stored) == len(turn.citations)

    def test_citations_point_at_real_evidence(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            turn = manager.ask("人均八十的茶餐厅")
            session.commit()

            for citation in turn.citations:
                if citation["evidence_id"]:
                    assert session.get(EvidenceUnit, citation["evidence_id"]) is not None
                if citation["source_id"]:
                    assert session.get(Source, citation["source_id"]) is not None
                # Timestamped precision must actually have a timestamp.
                if citation["precision"] == "evidence_timestamp":
                    assert citation["start_ms"] is not None

    def test_no_evidence_answer_is_honest(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            turn = manager.ask("我收藏里有关于量子计算硬件的内容吗？")
            session.commit()

            assert not turn.answer.has_evidence
            assert NO_EVIDENCE_TEMPLATE in turn.answer.content
            assert turn.answer.suggestions, "a no-result answer must offer next actions"
            assert not turn.citations, "no evidence means no citations"

    def test_general_scope_gets_no_collection_citations(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            turn = manager.ask("MCP 一般来说是什么？")
            session.commit()

            assert turn.answer.scope == "general"
            assert not turn.citations, "general knowledge must never carry collection provenance"

    def test_followup_resolves_against_previous_turn(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            first = manager.ask("我收藏里有哪些茶餐厅？")
            session.commit()

            second = manager.ask("这家有什么推荐菜？", conversation_id=first.conversation_id)
            session.commit()

            assert second.conversation_id == first.conversation_id
            assert second.resolved_query != "这家有什么推荐菜？", (
                "a referential follow-up must be expanded with prior context"
            )
            state = session.get(ConversationState, first.conversation_id)
            assert state is not None and state.state_json["recent_entities"]

    def test_history_round_trips(self, indexed, settings):
        factory, _ = indexed
        with factory() as session:
            manager = self._manager(session, settings)
            turn = manager.ask("人均多少？")
            session.commit()

            messages = manager.list_messages(turn.conversation_id)
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assistant = messages[1]
            assert assistant["content"] == turn.answer.content
            assert len(assistant["citations"]) == len(turn.citations)
            # Ordinals must survive the round trip or the markers in the stored
            # text stop matching the stored citations.
            assert [c["ordinal"] for c in assistant["citations"]] == sorted(
                c["ordinal"] for c in turn.citations
            )

    def test_model_hallucinated_markers_are_stripped(self, indexed, settings):
        """A model citing [99] when 3 sources were supplied must not leak it."""
        factory, _ = indexed

        class FakeChat:
            def generate(self, messages, *, temperature=0.7, max_tokens=None):
                from douyin_knowledge.ai.providers import ChatResponse

                return ChatResponse(
                    content="人均大概八十块[1]，另外据说还有分店[99]。",
                    model="fake-chat",
                )

        with factory() as session:
            manager = ConversationManager(
                session,
                vector_store=VectorStore(settings.vector_dir),
                embedder=MockEmbeddingModel(),
                chat_model=FakeChat(),
            )
            turn = manager.ask("人均八十的茶餐厅")
            session.commit()

            assert "[99]" not in turn.answer.content
            assert turn.answer.diagnostics["stripped_markers"] == [99]
