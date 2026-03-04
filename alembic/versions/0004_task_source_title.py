"""Add source_title to tasks."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_task_source_title"
down_revision = "0003_task_status_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("source_title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "source_title")
