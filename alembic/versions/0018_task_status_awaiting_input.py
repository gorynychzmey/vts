"""Add awaiting_input task status and Task.awaiting_step (vts-80i)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018_task_status_awaiting_input"
down_revision = "0017_match_decisions"
branch_labels = None
depends_on = None


OLD_TASK_STATUS = sa.Enum(
    "queued",
    "running",
    "waiting",
    "paused",
    "completed",
    "archived",
    "failed",
    "canceled",
    name="task_status",
    native_enum=False,
)

NEW_TASK_STATUS = sa.Enum(
    "queued",
    "running",
    "waiting",
    "paused",
    "completed",
    "archived",
    "failed",
    "canceled",
    "awaiting_input",
    name="task_status",
    native_enum=False,
)


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "status",
        existing_type=OLD_TASK_STATUS,
        type_=NEW_TASK_STATUS,
        existing_nullable=False,
    )
    op.add_column("tasks", sa.Column("awaiting_step", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "awaiting_step")
    op.alter_column(
        "tasks",
        "status",
        existing_type=NEW_TASK_STATUS,
        type_=OLD_TASK_STATUS,
        existing_nullable=False,
    )
