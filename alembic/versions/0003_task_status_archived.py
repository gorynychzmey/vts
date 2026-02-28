"""Add archived task status."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_task_status_archived"
down_revision = "0002_user_preferred_ytdlp_client"
branch_labels = None
depends_on = None


OLD_TASK_STATUS = sa.Enum(
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "canceled",
    name="task_status",
    native_enum=False,
)

NEW_TASK_STATUS = sa.Enum(
    "queued",
    "running",
    "paused",
    "completed",
    "archived",
    "failed",
    "canceled",
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


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "status",
        existing_type=NEW_TASK_STATUS,
        type_=OLD_TASK_STATUS,
        existing_nullable=False,
    )

