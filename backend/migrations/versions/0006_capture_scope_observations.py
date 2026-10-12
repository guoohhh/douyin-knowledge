"""Default-favorites capture observations and completion state.

Revision ID: 0006_capture_scope_observations
Revises: 0005_claim_grounding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_capture_scope_observations"
down_revision: str | None = "0005_claim_grounding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_default_favorite_observations",
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("first_seen_at_ms", sa.Integer(), nullable=False),
        sa.Column("last_seen_at_ms", sa.Integer(), nullable=False),
        sa.Column("is_present", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index(
        "ix_source_default_favorite_observations_present",
        "source_default_favorite_observations",
        ["is_present"],
    )
    op.create_table(
        "default_favorites_sync_state",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("last_completed_at_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("platform"),
    )


def downgrade() -> None:
    op.drop_table("default_favorites_sync_state")
    op.drop_index(
        "ix_source_default_favorite_observations_present",
        table_name="source_default_favorite_observations",
    )
    op.drop_table("source_default_favorite_observations")
