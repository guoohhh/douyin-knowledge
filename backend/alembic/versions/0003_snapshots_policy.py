"""Version captured source metadata and audit policy decisions.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column("content_checksum", sa.String(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("processed_checksum", sa.String(), nullable=False, server_default="")
        )
    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "checksum"),
    )
    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("policy_decisions")
    op.drop_table("source_snapshots")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("processed_checksum")
        batch.drop_column("content_checksum")
