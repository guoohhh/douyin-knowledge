"""fts5 virtual table, sync triggers and partial unique indexes

Alembic autogenerate cannot express any of this, so it lives in hand-written DDL.

Tokenizer decision (deviation from PHYSICAL_SCHEMA section 42, which suggests bare
``unicode61`` and explicitly asks for it to be benchmarked before freezing):

``unicode61`` treats an unsegmented Chinese sentence as ONE token, so
``人均`` matches nothing in ``这家店人均八十块钱``. ``trigram`` fixes 3-character
queries but returns zero rows for 2-character queries, and 2-character terms
(人均 / 日料 / 好吃) are the dominant query shape for this product. Measured on
SQLite 3.37 with the project's own fixture text.

Resolution: keep ``unicode61`` and pre-segment CJK with jieba at both index and
query time (see search/tokenizer.py). ``remove_diacritics 2`` additionally makes
Latin-script queries accent-insensitive. Recorded as DEC-C10 in docs/DECISIONS.md.

Revision ID: 0002_fts_and_partial_indexes
Revises: 0001_core_schema
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_fts_and_partial_indexes"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FTS_CREATE = """
CREATE VIRTUAL TABLE search_fts USING fts5(
  title,
  body,
  content='search_documents',
  content_rowid='rowid',
  tokenize="unicode61 remove_diacritics 2"
)
"""

# External-content FTS5 tables are not automatically kept in sync. Triggers are the
# only way to guarantee the index cannot drift from search_documents even if a write
# bypasses the repository layer.
TRIGGERS = [
    """
    CREATE TRIGGER search_documents_ai AFTER INSERT ON search_documents BEGIN
      INSERT INTO search_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
    END
    """,
    """
    CREATE TRIGGER search_documents_ad AFTER DELETE ON search_documents BEGIN
      INSERT INTO search_fts(search_fts, rowid, title, body)
      VALUES ('delete', old.rowid, old.title, old.body);
    END
    """,
    """
    CREATE TRIGGER search_documents_au AFTER UPDATE ON search_documents BEGIN
      INSERT INTO search_fts(search_fts, rowid, title, body)
      VALUES ('delete', old.rowid, old.title, old.body);
      INSERT INTO search_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
    END
    """,
]

# Only one revision per wiki page may be current (PHYSICAL_SCHEMA section 34).
WIKI_CURRENT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_wiki_revision_current
ON wiki_revisions(page_id)
WHERE is_current = 1
"""

# A source may have at most one non-terminal processing run at a time. Without this,
# a duplicated process_source job can create two concurrent runs and the
# current_processing_run_id pointer becomes a race.
ACTIVE_RUN_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_processing_runs_active
ON processing_runs(source_id)
WHERE status IN ('queued', 'running')
"""


def upgrade() -> None:
    op.execute(FTS_CREATE)
    for trigger in TRIGGERS:
        op.execute(trigger)
    op.execute(WIKI_CURRENT_INDEX)
    op.execute(ACTIVE_RUN_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_processing_runs_active")
    op.execute("DROP INDEX IF EXISTS ux_wiki_revision_current")
    for name in ("search_documents_au", "search_documents_ad", "search_documents_ai"):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS search_fts")
