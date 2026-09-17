"""Programmatic migration entry point.

The API, the CLI and the test suite all call :func:`upgrade_to_head` so there is
exactly one way the schema gets created. ``alembic upgrade head`` on the command line
goes through the same env.py.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.db.engine import create_db_engine
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(settings: Settings) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.resolved_database_url)
    cfg.set_main_option("sqlalchemy.url_override", settings.resolved_database_url)
    return cfg


def upgrade_to_head(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    settings.ensure_directories()
    cfg = alembic_config(settings)
    engine = create_db_engine(settings)
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        revision = current_revision(engine) or "unknown"
    finally:
        engine.dispose()
    logger.info("database migrated", extra={"revision": revision})
    return revision


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    script = ScriptDirectory.from_config(alembic_config(settings))
    head = script.get_current_head()
    return head or "unknown"


def pending_migrations(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    engine = create_db_engine(settings)
    try:
        return current_revision(engine) != head_revision(settings)
    finally:
        engine.dispose()
