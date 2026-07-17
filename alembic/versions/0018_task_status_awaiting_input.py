"""Add awaiting_input task status and Task.awaiting_step (vts-80i).

NOTE: down_revision is temporarily pinned to 0016_voice_samples because
0017_match_decisions (Task 9 of the vts-80i plan) has not landed yet. When
Task 9 lands, repoint this migration's down_revision to
"0017_match_decisions" so the chain is: 0016 -> 0017 -> 0018.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018_task_status_awaiting_input"
down_revision = "0016_voice_samples"  # TODO(vts-80i task 9): repoint to 0017_match_decisions
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
