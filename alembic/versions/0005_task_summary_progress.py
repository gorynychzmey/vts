"""Add summary_progress to tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_task_summary_progress"
down_revision = "0004_task_source_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("summary_progress", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "summary_progress")
