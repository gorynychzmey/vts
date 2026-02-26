"""Initial schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "paused",
                "completed",
                "failed",
                "canceled",
                name="task_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("artifact_dir", sa.Text(), nullable=False),
        sa.Column("transcript_path", sa.Text(), nullable=True),
        sa.Column("summary_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_user_created", "tasks", ["user_id", "created_at"], unique=False)
    op.create_index("ix_tasks_status_created", "tasks", ["status", "created_at"], unique=False)

    op.create_table(
        "steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "skipped",
                name="step_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "name", name="uq_steps_task_name"),
    )
    op.create_index("ix_steps_task_status", "steps", ["task_id", "status"], unique=False)

    op.create_table(
        "asr_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "segment_index", name="uq_asr_segments_task_segment"),
    )
    op.create_index("ix_asr_segments_task_start", "asr_segments", ["task_id", "start_sec"], unique=False)

    op.create_table(
        "asr_words",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("word", sa.String(length=128), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["segment_id"], ["asr_segments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asr_words_task_start", "asr_words", ["task_id", "start_sec"], unique=False)
    op.create_index("ix_asr_words_segment", "asr_words", ["segment_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_asr_words_segment", table_name="asr_words")
    op.drop_index("ix_asr_words_task_start", table_name="asr_words")
    op.drop_table("asr_words")

    op.drop_index("ix_asr_segments_task_start", table_name="asr_segments")
    op.drop_table("asr_segments")

    op.drop_index("ix_steps_task_status", table_name="steps")
    op.drop_table("steps")

    op.drop_index("ix_tasks_status_created", table_name="tasks")
    op.drop_index("ix_tasks_user_created", table_name="tasks")
    op.drop_table("tasks")

    op.drop_table("users")

    sa.Enum(name="task_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="step_status").drop(op.get_bind(), checkfirst=True)

