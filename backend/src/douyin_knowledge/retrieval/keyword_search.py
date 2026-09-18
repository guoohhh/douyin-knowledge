"""FTS5 keyword search over ``search_documents``.

Queries run against the ``search_fts`` external-content virtual table created in
migration 0002. Two things are non-obvious and load-bearing:

* **Both sides go through the same segmenter.** The indexer writes jieba-spaced
  text; queries are segmented by `build_match_query` before MATCH. Skipping
  either half silently returns zero rows for Chinese input (DEC-C10).
* **BM25 sign.** SQLite's ``bm25()`` returns *negative* numbers where more
  negative means more relevant. We negate it so callers can treat every score
  in the system as "higher is better", which is what score fusion assumes.

Results carry the un-segmented ``raw_text`` from metadata, because the spaced
form is an index artifact and must never reach the user or an LLM prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text as sql_text

from douyin_knowledge.observability.logging import get_logger
from douyin_knowledge.search.tokenizer import build_match_query

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

logger = get_logger(__name__)

# Weights per FTS5 column (title, body): a title match is worth more than a
# body match, which matters for "京都" hitting a titled itinerary video over a
# passing mention in someone's restaurant review.
_COLUMN_WEIGHTS = (5.0, 1.0)


@dataclass(frozen=True)
class KeywordHit:
    doc_type: str
    object_id: str
    title: str | None
    snippet: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_id(self) -> str | None:
        value = self.metadata.get("source_id")
        return str(value) if value else None


class KeywordSearcher:
    """Thin, explicit wrapper over the FTS5 table.

    Raw SQL is used deliberately: FTS5 MATCH, ``bm25()`` and ``snippet()`` have
    no SQLAlchemy ORM equivalent, and expressing them through the ORM would add
    indirection without removing the SQL.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def available(self) -> bool:
        """Whether the FTS table exists — false on a DB predating migration 0002."""
        row = self.session.execute(
            sql_text("SELECT name FROM sqlite_master WHERE type='table' AND name='search_fts'")
        ).first()
        return row is not None

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        doc_types: Sequence[str] | None = None,
        allowed_object_ids: Sequence[str] | None = None,
        mode: str = "and",
    ) -> list[KeywordHit]:
        """Run a MATCH query and return hits ordered by relevance.

        Falls back from AND to OR when a multi-token AND query finds nothing:
        a user typing "港大 附近 日料" usually wants the best partial match
        rather than an empty page.
        """
        match_query = build_match_query(query, mode=mode)
        if not match_query:
            return []

        hits = self._execute(match_query, limit, doc_types, allowed_object_ids)
        if not hits and mode == "and":
            or_query = build_match_query(query, mode="or")
            if or_query and or_query != match_query:
                hits = self._execute(or_query, limit, doc_types, allowed_object_ids)
        return hits

    def _execute(
        self,
        match_query: str,
        limit: int,
        doc_types: Sequence[str] | None,
        allowed_object_ids: Sequence[str] | None,
    ) -> list[KeywordHit]:
        params: dict[str, Any] = {"match": match_query, "limit": limit}
        clauses = ["search_fts MATCH :match"]

        if doc_types:
            names = []
            for index, doc_type in enumerate(doc_types):
                key = f"dt{index}"
                params[key] = doc_type
                names.append(f":{key}")
            clauses.append(f"d.doc_type IN ({', '.join(names)})")

        if allowed_object_ids is not None:
            if not allowed_object_ids:
                return []  # an empty allow-list means nothing is retrievable
            names = []
            for index, object_id in enumerate(allowed_object_ids):
                key = f"oid{index}"
                params[key] = object_id
                names.append(f":{key}")
            clauses.append(f"d.object_id IN ({', '.join(names)})")

        statement = sql_text(
            f"""
            SELECT
                d.doc_type      AS doc_type,
                d.object_id     AS object_id,
                d.title         AS title,
                d.metadata_json AS metadata_json,
                snippet(search_fts, 1, '<mark>', '</mark>', ' … ', 24) AS snippet,
                bm25(search_fts, {_COLUMN_WEIGHTS[0]}, {_COLUMN_WEIGHTS[1]}) AS rank_score
            FROM search_fts
            JOIN search_documents d ON d.rowid = search_fts.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY rank_score
            LIMIT :limit
            """
        )

        try:
            rows = self.session.execute(statement, params).mappings().all()
        except Exception as exc:  # noqa: BLE001 - a malformed MATCH must not 500
            logger.warning(
                "fts_query_failed", extra={"match": match_query, "error": str(exc)}
            )
            return []

        hits: list[KeywordHit] = []
        for row in rows:
            metadata = self._coerce_metadata(row["metadata_json"])
            raw_text = metadata.get("raw_text")
            snippet = row["snippet"] or ""
            hits.append(
                KeywordHit(
                    doc_type=row["doc_type"],
                    object_id=row["object_id"],
                    title=row["title"],
                    # Prefer the unsegmented original; the FTS snippet contains
                    # jieba spacing that reads as broken Chinese.
                    snippet=str(raw_text) if raw_text else snippet.replace(" ", ""),
                    score=-float(row["rank_score"] or 0.0),
                    metadata=metadata,
                )
            )
        return hits

    @staticmethod
    def _coerce_metadata(value: Any) -> dict[str, Any]:
        """metadata_json arrives as dict or str depending on the driver path."""
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def count_documents(self, doc_type: str | None = None) -> int:
        if doc_type:
            row = self.session.execute(
                sql_text("SELECT COUNT(*) FROM search_documents WHERE doc_type = :dt"),
                {"dt": doc_type},
            ).scalar()
        else:
            row = self.session.execute(sql_text("SELECT COUNT(*) FROM search_documents")).scalar()
        return int(row or 0)


__all__ = ["KeywordHit", "KeywordSearcher"]
