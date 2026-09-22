"""Merged entities are redirects, not results.

A merge consolidates two records of one restaurant: the dead entity keeps its claims
(that is the point -- the survivor qualifies on the evidence the merge gathered) while the
*name the user sees* must be the survivor's. Returning the deprecated entity names a page
the rest of the product (wiki builder, search indexer, Knowledge API detail route) already
treats as a redirect, so the same restaurant appears under a name no other surface uses.

Everything here keys off ``merged_into_entity_id``, never off ``status`` alone: no CHECK
constraint ties the two together, so a merge that set the pointer and not the status is a
shape the database permits and the executor has to survive.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from douyin_knowledge.db.engine import sqlite_file_of
from douyin_knowledge.db.models.entities import Claim, Entity
from douyin_knowledge.retrieval.structured import StructuredExecutor
from tests.test_structured_retrieval import (
    CANONICAL_QUERY,
    Corpus,
    _index,
    _names,
    _plan,
    corpus,  # noqa: F401 - fixture
)


def _merge(session: Session, dead: Entity, survivor: Entity) -> None:
    """Point `dead` at `survivor` the way a real merge does: pointer *and* status."""
    dead.merged_into_entity_id = survivor.id
    dead.status = "merged"
    session.flush()


def _ask(session: Session, query: str = CANONICAL_QUERY, **kwargs):
    """The product entry point, so assertions land on rendered answer text."""
    from douyin_knowledge.conversation.conversation_manager import ConversationManager

    return ConversationManager(session).ask(query, **kwargs)


def _source_ids_of(session: Session, entity: Entity) -> set[str]:
    return {
        claim.source_id
        for claim in session.scalars(select(Claim).where(Claim.subject_entity_id == entity.id))
    }


class TestMergedEntitiesAreNotReturnedAsResults:
    def test_the_survivor_answers_on_the_merged_entitys_claims(
        self, session: Session, corpus: Corpus  # noqa: F811
    ) -> None:
        """The dead entity qualifies; the answer names the survivor and keeps the provenance.

        Provenance is the load-bearing half. Dropping the merged entity's claims would make
        the survivor unqualifiable and the answer uncitable -- it would delete exactly the
        evidence the merge existed to consolidate.
        """
        dead = corpus.restaurant(
            "旺角旧名店",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="一家旺角的日料店，人均80，很地道",
        )
        survivor = corpus.entity("旺角新名店")
        _merge(session, dead, survivor)
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角新名店"]
        # Provenance survives the redirect: the sources behind the match are the dead
        # entity's sources, because those are the only claims that exist.
        assert set(result.qualifying_source_ids) == _source_ids_of(session, dead)
        supports = result.matches[0].supports
        assert set(supports) == {"district", "cuisine", "price_per_person"}
        for field_name, support in supports.items():
            assert support.satisfied_by, f"{field_name} lost its satisfying claim"

        body = _ask(session).as_dict()

        assert "旺角新名店" in body["content"]
        assert "旧名店" not in body["content"], "a merged entity must not be named"
        assert body["citations"], "the survivor must still be citable"

    def test_no_duplicate_when_both_the_dead_entity_and_its_survivor_qualify(
        self, session: Session, corpus: Corpus  # noqa: F811
    ) -> None:
        """One restaurant, one result -- with both sides' claims behind it."""
        dead = corpus.restaurant("旺角旧名店", district="旺角", cuisine="日料", price=80)
        survivor = corpus.restaurant("旺角新名店", district="旺角", cuisine="日料", price=90)
        _merge(session, dead, survivor)
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角新名店"]
        assert set(result.qualifying_source_ids) == _source_ids_of(
            session, dead
        ) | _source_ids_of(session, survivor)

        body = _ask(session).as_dict()

        assert body["content"].count("旺角新名店") >= 1
        assert "旧名店" not in body["content"]
        assert body["meta"]["diagnostics"]["structured"]["matched"] == 1

    def test_no_duplicate_when_two_dead_entities_redirect_into_one_survivor(
        self, session: Session, corpus: Corpus  # noqa: F811
    ) -> None:
        dead_a = corpus.restaurant("旺角旧名甲", district="旺角", cuisine="日料", price=80)
        dead_b = corpus.restaurant("旺角旧名乙", district="旺角", cuisine="日料", price=85)
        survivor = corpus.entity("旺角新名店")
        _merge(session, dead_a, survivor)
        _merge(session, dead_b, survivor)
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角新名店"]
        assert set(result.qualifying_source_ids) == _source_ids_of(
            session, dead_a
        ) | _source_ids_of(session, dead_b)
        # Both sides' price claims reach the match, so the disagreement the merge inherited
        # is still visible rather than silently resolved to whichever id sorted first.
        assert sorted(result.matches[0].supports["price_per_person"].conflicting_values) == [
            80.0,
            85.0,
        ]

        body = _ask(session).as_dict()

        assert body["content"].count("旺角新名店") >= 1
        assert "旧名" not in body["content"]
        assert body["meta"]["diagnostics"]["structured"]["matched"] == 1

    def test_a_multi_hop_merge_chain_resolves_to_the_final_survivor(
        self, session: Session, corpus: Corpus  # noqa: F811
    ) -> None:
        """A -> B -> C returns C. A single JOIN would return the mid-chain corpse B."""
        a = corpus.restaurant("旺角一代店", district="旺角", cuisine="日料", price=80)
        b = corpus.entity("旺角二代店")
        c = corpus.entity("旺角三代店")
        _merge(session, a, b)
        _merge(session, b, c)
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角三代店"]

        body = _ask(session).as_dict()

        assert "旺角三代店" in body["content"]
        assert "旺角二代店" not in body["content"], "a mid-chain entity is still a corpse"
        assert "旺角一代店" not in body["content"]

    def test_a_dangling_merge_pointer_does_not_crash(
        self, session: Session, corpus: Corpus, engine: Engine  # noqa: F811
    ) -> None:
        """Pointer at a nonexistent entity: behave like `_follow_merge`, last reachable wins.

        Returning nothing would silently drop a restaurant the user really saved because of
        a broken pointer, so the last entity that actually exists is the answer.
        """
        dead = corpus.restaurant("旺角孤儿店", district="旺角", cuisine="日料", price=80)
        session.commit()
        # The FK constraint is enforced, so bypassing it requires a raw sqlite3 connection
        # with foreign_keys=OFF. This shape only happens if the DB is corrupted, but the
        # executor must still not crash -- and _follow_merge already has the rule.
        path = sqlite_file_of(engine)
        assert path is not None
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "UPDATE entities SET merged_into_entity_id='entity-that-does-not-exist', status='merged' WHERE id=?",
            (dead.id,),
        )
        raw.commit()
        raw.close()
        session.expire_all()
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角孤儿店"]
        assert set(result.qualifying_source_ids) == _source_ids_of(session, dead)

        body = _ask(session).as_dict()

        assert "旺角孤儿店" in body["content"]

    def test_an_unmerged_entity_is_unaffected(
        self, session: Session, corpus: Corpus  # noqa: F811
    ) -> None:
        """Control: the ordinary path keeps its own identity and its own claims."""
        plain = corpus.restaurant(
            "旺角松本食堂",
            district="旺角",
            cuisine="日料",
            price=80,
            evidence_text="旺角松本食堂 人均80 很地道的日料",
        )
        _index(session)

        result = StructuredExecutor(session).execute(_plan())

        assert _names(result) == ["旺角松本食堂"]
        assert result.matches[0].entity.id == plain.id
        assert set(result.qualifying_source_ids) == _source_ids_of(session, plain)

        body = _ask(session).as_dict()

        assert "旺角松本食堂" in body["content"]
        assert body["citations"]
