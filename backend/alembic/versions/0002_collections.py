"""Preserve many-to-many source collection memberships.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_collection_memberships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collection_name", sa.String(), nullable=False),
        sa.UniqueConstraint("source_id", "collection_name"),
    )
    op.execute(
        "INSERT INTO source_collection_memberships(id,source_id,collection_name) SELECT lower(hex(randomblob(16))),id,collection FROM sources WHERE collection <> ''"
    )


def downgrade():
    op.drop_table("source_collection_memberships")
