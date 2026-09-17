"""Session management.

Local-first single-user app: one process-wide engine, short-lived sessions.
FastAPI dependencies and the worker both go through :func:`session_scope`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.db.engine import create_db_engine

_lock = threading.Lock()
_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def init_engine(settings: Settings | None = None, *, force: bool = False) -> Engine:
    global _engine, _factory
    with _lock:
        if _engine is not None and not force:
            return _engine
        if _engine is not None and force:
            _engine.dispose()
        settings = settings or get_settings()
        _engine = create_db_engine(settings)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        return _engine


def get_engine() -> Engine:
    return _engine or init_engine()


def get_session_factory() -> sessionmaker[Session]:
    if _factory is None:
        init_engine()
    assert _factory is not None
    return _factory


def new_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    global _engine, _factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _factory = None
