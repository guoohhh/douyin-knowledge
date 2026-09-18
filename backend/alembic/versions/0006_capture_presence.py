"""Track capture ownership to reconcile removed sidecar saves.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sources") as batch:
        batch.add_column(
            sa.Column("capture_origin", sa.String(), nullable=False, server_default="import")
        )


def downgrade():
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("capture_origin")
