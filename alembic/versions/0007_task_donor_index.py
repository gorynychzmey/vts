"""Add index on tasks(source_url, status) for donor lookup."""

from __future__ import annotations

from alembic import op

revision = "0007_task_donor_index"
down_revision = "0006_drop_asr_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_source_url_status", "tasks", ["source_url", "status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_source_url_status", table_name="tasks")
