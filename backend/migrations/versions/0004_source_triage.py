"""cache cheap content-type triage so semantic policy rules can match

``RuleType.SEMANTIC`` has been accepted, stored and listed since the first migration
while never matching anything: the evaluator skipped it because a content-type label did
not exist anywhere. This revision gives that label a home.

The table is *derived* state, not source of truth. Every row can be recomputed from the
source's metadata, and ``signal_fingerprint`` records which metadata it was computed
from, so a re-synced source whose title and tags did not change is not reclassified (and,
where a model was used, not re-paid for) while one that was edited is.

One row per source. History is deliberately not kept here: unlike a ProcessingRun, a
triage label is a routing hint with no provenance value, and the decision it influenced
is already recorded in ``policy_decisions``. Keeping versions of it would imply the label
itself is knowledge, which PROCESSING_POLICY.md 5.4 explicitly says it is not.

Revision ID: 0004_source_triage
Revises: 0003_media_acquisition_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_source_triage"
down_revision: str | None = "0003_media_acquisition_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_triage",
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        # `cues` or `model`. Worth a column rather than inferring it from `model_name`,
        # because "the cues were inconclusive and no model was configured" and "a model
        # said unknown" are different situations for the user to act on.
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("cues_json", sa.JSON(), nullable=True),
        sa.Column("signal_fingerprint", sa.Text(), nullable=False),
        sa.Column("computed_at_ms", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )
    # The lookup that matters is "which sources are entertainment clips", both for the
    # semantic rule evaluation and for the UI filter.
    op.create_index("ix_source_triage_content_type", "source_triage", ["content_type"])


def downgrade() -> None:
    op.drop_index("ix_source_triage_content_type", table_name="source_triage")
    op.drop_table("source_triage")
