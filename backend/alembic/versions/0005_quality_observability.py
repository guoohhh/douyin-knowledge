"""Persist processing model/output and Wiki quality events.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("processing_runs") as batch:
        batch.add_column(sa.Column("model_name", sa.String(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("result_summary", sa.JSON(), nullable=False, server_default="{}")
        )
    op.create_table(
        "wiki_quality_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "page_id",
            sa.String(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issue", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "sync_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("sync_events")
    op.drop_table("wiki_quality_events")
    with op.batch_alter_table("processing_runs") as batch:
        batch.drop_column("result_summary")
        batch.drop_column("model_name")
