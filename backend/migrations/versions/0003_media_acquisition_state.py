"""separate remote media URLs from local storage, and track acquisition state

Before this revision ``source_assets.storage_key`` held whatever the capture provider
handed over, which for the real Douyin provider was an absolute signed URL. The
orchestrator resolves ASR input as ``media_dir / storage_key``, so a URL there produced
a path that could never exist and every real source recorded
``asr_skipped_media_not_downloaded``. Demo mode hid it: the fixtures carry inline
subtitles and never need ASR (DEC-014).

The fix is one column per concept -- ``remote_url`` for provenance, ``storage_key`` for
the local relative path, ``download_state`` for whether the bytes actually arrived --
rather than one column asked to mean all three.

Existing rows are migrated, not dropped. Any row whose ``storage_key`` parses as an
absolute URL has that value moved into ``remote_url``, is given a computed local key,
and is marked ``pending`` so the acquisition job picks it up. Nothing is lost, and no
row is left claiming a local file that was never there.

Revision ID: 0003_media_acquisition_state
Revises: 0002_fts_and_partial_indexes
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0003_media_acquisition_state"
down_revision: str | None = "0002_fts_and_partial_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_SUFFIXES = {
    ".mp4", ".m4a", ".mp3", ".aac", ".wav", ".webm", ".mov", ".ogg", ".flac", ".opus", ".bin",
}

# Duplicated from media/store.py on purpose. A migration is a historical record: if the
# helper is later renamed or its fingerprint basis changes, this revision must still
# reproduce the keys it wrote on the day it ran.
def _fingerprint(url: str) -> str:
    parts = urlsplit(url)
    basis = f"{(parts.hostname or '').lower()}{parts.path}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _segment(value: str, fallback: str) -> str:
    cleaned = _UNSAFE.sub("_", value or "").strip("._")
    return cleaned[:96] or fallback


def _suffix(url: str) -> str:
    candidate = (urlsplit(url).path.rsplit(".", 1) + [""])[1].lower()
    dotted = f".{candidate}" if candidate else ""
    return dotted if dotted in _SAFE_SUFFIXES else ".bin"


def upgrade() -> None:
    with op.batch_alter_table("source_assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("remote_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("remote_url_fingerprint", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "download_state",
                sa.Text(),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column(
                "download_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("downloaded_at_ms", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("download_error", sa.Text(), nullable=True))

    _backfill()

    with op.batch_alter_table("source_assets", schema=None) as batch_op:
        batch_op.create_index("ix_source_assets_download_state", ["download_state"], unique=False)
        batch_op.create_index(
            "ix_source_assets_identity",
            ["source_id", "asset_type", "remote_url_fingerprint"],
            unique=False,
        )


def _backfill() -> None:
    """Move URL-shaped storage keys into remote_url and compute real local keys."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT a.id, a.storage_key, a.asset_type, a.retention_class, s.platform,"
            " s.external_id"
            " FROM source_assets AS a JOIN sources AS s ON s.id = a.source_id"
        )
    ).fetchall()

    for row in rows:
        key = row.storage_key or ""
        scheme = (urlsplit(key).scheme or "").lower()
        if scheme not in ("http", "https"):
            # Already a relative key. It may or may not exist on disk; `pending` is the
            # honest default, and the acquisition job re-checks presence before
            # spending a download.
            continue
        digest = _fingerprint(key)
        local = "/".join(
            (
                "pinned" if row.retention_class == "retained" else "cache",
                _segment(row.platform, "unknown"),
                _segment(row.external_id, "unknown"),
                f"{_segment(row.asset_type, 'asset')}-{digest[:16]}{_suffix(key)}",
            )
        )
        bind.execute(
            sa.text(
                "UPDATE source_assets SET remote_url = :url, remote_url_fingerprint = :fp,"
                " storage_key = :key, download_state = 'pending' WHERE id = :id"
            ),
            {"url": key, "fp": digest, "key": local, "id": row.id},
        )


def downgrade() -> None:
    # Restore the old single-column meaning so the previous revision's code still works:
    # the URL goes back into storage_key where it came from.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE source_assets SET storage_key = remote_url"
            " WHERE remote_url IS NOT NULL AND remote_url <> ''"
        )
    )
    with op.batch_alter_table("source_assets", schema=None) as batch_op:
        batch_op.drop_index("ix_source_assets_identity")
        batch_op.drop_index("ix_source_assets_download_state")
        batch_op.drop_column("download_error")
        batch_op.drop_column("downloaded_at_ms")
        batch_op.drop_column("download_attempts")
        batch_op.drop_column("download_state")
        batch_op.drop_column("remote_url_fingerprint")
        batch_op.drop_column("remote_url")
