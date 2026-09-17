"""Track transient source media locations for optional enrichment.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "kind"),
    )


def downgrade():
    op.drop_table("source_assets")
