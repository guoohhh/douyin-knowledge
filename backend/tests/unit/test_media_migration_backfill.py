"""Migration 0003 must rescue existing rows, not abandon them.

A user who already synced with the real provider has rows whose ``storage_key`` is a
signed URL. The migration has to move that value somewhere honest and leave the row
fetchable. Testing it means driving alembic across the boundary rather than against
``head``, because at ``head`` the old shape is unrepresentable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from douyin_knowledge.config import Settings

LEGACY_URL = "https://v26.douyinvod.com/hashA/video.mp4?sign=aaa&expire=1"


def migrate_to(settings: Settings, revision: str) -> None:
    """Move this settings object's database to an exact revision.

    The connection is created here and handed to alembic via ``cfg.attributes``, the same
    way ``upgrade_to_head`` does it. That detail is load-bearing: env.py's online path
    ignores ``sqlalchemy.url_override`` and builds an engine from the *ambient* settings,
    so a config-only approach silently migrates the developer's real database and leaves
    the test's tmp_path empty.
    """
    from alembic import command

    from douyin_knowledge.db.engine import create_db_engine
    from douyin_knowledge.db.migrate import alembic_config

    cfg = alembic_config(settings)
    engine = create_db_engine(settings)
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            if revision == "downgrade_0002":
                command.downgrade(cfg, "0002_fts_and_partial_indexes")
            else:
                command.upgrade(cfg, revision)
    finally:
        engine.dispose()


@pytest.fixture
def at_0002(tmp_path: Path) -> Settings:
    """A database stopped one revision before the one under test."""
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    migrate_to(settings, "0002_fts_and_partial_indexes")
    return settings


def seed_legacy_asset(db_path: Path, storage_key: str, *, retention: str = "cache") -> None:
    """Insert a source and an asset in the pre-0003 shape."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO sources (id, platform, external_id, source_type, availability,"
            " first_seen_at_ms, last_seen_at_ms)"
            " VALUES ('src_1', 'douyin', '7100', 'video', 'available', 1, 1)"
        )
        conn.execute(
            "INSERT INTO source_assets (id, source_id, asset_type, retention_class,"
            " storage_key, created_at_ms) VALUES ('ast_1', 'src_1', 'video', ?, ?, 1)",
            (retention, storage_key),
        )
    conn.close()


def read_asset(db_path: Path) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM source_assets WHERE id = 'ast_1'").fetchone()
    conn.close()
    return row


class TestBackfill:
    def test_url_moves_out_of_storage_key(self, at_0002: Settings) -> None:
        settings = at_0002
        seed_legacy_asset(settings.db_path, LEGACY_URL)

        migrate_to(settings, "0003_media_acquisition_state")

        row = read_asset(settings.db_path)
        assert row["remote_url"] == LEGACY_URL
        assert row["remote_url_fingerprint"]
        # The whole point: storage_key now names a local, relative destination.
        assert not row["storage_key"].startswith("http")
        assert row["storage_key"].startswith("cache/douyin/7100/")
        # And it is queued for acquisition rather than silently assumed present.
        assert row["download_state"] == "pending"

    def test_retained_asset_lands_in_pinned(self, at_0002: Settings) -> None:
        settings = at_0002
        seed_legacy_asset(settings.db_path, LEGACY_URL, retention="retained")
        migrate_to(settings, "0003_media_acquisition_state")
        assert read_asset(settings.db_path)["storage_key"].startswith("pinned/")

    def test_migration_key_matches_the_runtime_helper(self, at_0002: Settings) -> None:
        """The migration duplicates the key-building logic on purpose, so it must be
        checked against the live implementation. If they diverge, a migrated row points
        at one path while every later sync computes another, and the file is fetched
        twice under two names."""
        settings = at_0002
        seed_legacy_asset(settings.db_path, LEGACY_URL)
        migrate_to(settings, "0003_media_acquisition_state")

        from douyin_knowledge.media.store import MediaStore

        expected = MediaStore(settings.media_dir).build_storage_key(
            platform="douyin",
            external_id="7100",
            asset_type="video",
            url=LEGACY_URL,
        )
        assert read_asset(settings.db_path)["storage_key"] == expected

    def test_already_relative_key_is_left_alone(self, at_0002: Settings) -> None:
        settings = at_0002
        seed_legacy_asset(settings.db_path, "cache/douyin/7100/video-abc.mp4")
        migrate_to(settings, "0003_media_acquisition_state")

        row = read_asset(settings.db_path)
        assert row["storage_key"] == "cache/douyin/7100/video-abc.mp4"
        assert row["remote_url"] is None

    def test_downgrade_restores_the_old_meaning(self, at_0002: Settings) -> None:
        """A downgrade must leave the previous revision's code working, which means the
        URL goes back where that code expects to read it."""
        settings = at_0002
        seed_legacy_asset(settings.db_path, LEGACY_URL)
        migrate_to(settings, "0003_media_acquisition_state")
        migrate_to(settings, "downgrade_0002")

        conn = sqlite3.connect(settings.db_path)
        key = conn.execute("SELECT storage_key FROM source_assets WHERE id='ast_1'").fetchone()[0]
        columns = {r[1] for r in conn.execute("PRAGMA table_info(source_assets)")}
        conn.close()
        assert key == LEGACY_URL
        assert "download_state" not in columns
