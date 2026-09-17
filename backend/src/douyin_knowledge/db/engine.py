"""Engine creation with the mandated SQLite runtime PRAGMAs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from douyin_knowledge.config import Settings

_REGISTERED: set[int] = set()


def _install_pragmas(engine: Engine, settings: Settings) -> None:
    if id(engine) in _REGISTERED:
        return
    _REGISTERED.add(id(engine))

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute(f"PRAGMA journal_mode = {settings.sqlite_journal_mode}")
            cursor.execute(f"PRAGMA synchronous = {settings.sqlite_synchronous}")
            cursor.execute(f"PRAGMA busy_timeout = {settings.sqlite_busy_timeout_ms}")
            # FTS5 external-content tables need this to stay consistent under WAL.
            cursor.execute("PRAGMA recursive_triggers = ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn: object) -> None:
        # SQLAlchemy's default deferred BEGIN turns writer contention into
        # "database is locked" at COMMIT time instead of at BEGIN time, where
        # busy_timeout can actually help. Job queue correctness depends on this.
        conn.exec_driver_sql("BEGIN IMMEDIATE")  # type: ignore[attr-defined]


def create_db_engine(settings: Settings) -> Engine:
    url = settings.resolved_database_url
    in_memory = ":memory:" in url

    if not in_memory:
        db_path = settings.db_path
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, object] = {
        "echo": settings.sql_echo,
        "future": True,
        # We manage transactions explicitly; SQLAlchemy must not emit its own BEGIN.
        "connect_args": {"check_same_thread": False, "isolation_level": None},
    }
    if in_memory:
        # A shared in-memory database must reuse one connection or every session
        # sees an empty schema.
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
    _install_pragmas(engine, settings)
    return engine


def sqlite_file_of(engine: Engine) -> Path | None:
    raw = engine.url.database
    if not raw or raw == ":memory:":
        return None
    return Path(raw)
