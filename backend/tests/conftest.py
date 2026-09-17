"""Shared fixtures.

Every test runs against a real migrated SQLite file in a tmp dir, not
``create_all``: migrations are part of the product, so if a migration is broken the
whole suite should fail rather than passing against a phantom schema.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from douyin_knowledge.config import Settings, reset_settings_cache
from douyin_knowledge.db import dispose_engine, init_engine, session_scope


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray DK_* var in the developer's shell must not change test behaviour."""
    for key in list(os.environ):
        if key.startswith("DK_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.ensure_directories()
    return s


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    from douyin_knowledge.db.migrate import upgrade_to_head

    upgrade_to_head(settings)
    eng = init_engine(settings, force=True)
    yield eng
    dispose_engine()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with session_scope() as s:
        yield s
