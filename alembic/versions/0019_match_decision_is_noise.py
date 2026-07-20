"""Add MatchDecision.is_noise (vts-552)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0019_match_decision_is_noise"
down_revision = "0018_task_status_awaiting_input"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "match_decisions",
        sa.Column("is_noise", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("match_decisions", "is_noise")
