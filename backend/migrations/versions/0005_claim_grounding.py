"""claim grounding validation audit trail

Revision ID: 0005_claim_grounding
Revises: 0004_source_triage
Create Date: 2026-09-18

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_claim_grounding'
down_revision: str | None = '0004_source_triage'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add grounding_status and grounding_json to claims for P1-2 validation audit."""
    op.add_column('claims', sa.Column('grounding_status', sa.Text(), nullable=True))
    op.add_column('claims', sa.Column('grounding_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('claims', 'grounding_json')
    op.drop_column('claims', 'grounding_status')
