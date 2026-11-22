"""drop the task's content columns; the recording holds the content (vts-lzad)

Upgrade drops `tasks.transcript_path` and `tasks.summary_path` and strips
`prompt_results` from every `tasks.options` and `recordings.meta`. Nothing in
the application reads them any more: the recording's own columns are the
content.

Downgrade is the rollback path to 1.10.1, whose code still reads those
fields and copies them onto the recording on every upsert. It re-adds the
columns and refills them, and both `prompt_results` copies, from each task's
recording. See vts/services/recording_backfill.py.

Revision ID: 0036_drop_task_content_columns
Revises: 0035_recording_listed_at
"""
import sqlalchemy as sa
from alembic import op

revision = "0036_drop_task_content_columns"
down_revision = "0035_recording_listed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from vts.services.recording_backfill import strip_task_content

    counts = strip_task_content(op.get_bind())
    op.drop_column("tasks", "transcript_path")
    op.drop_column("tasks", "summary_path")
    print(
        f"task content: prompt_results stripped from {counts['task_options']} tasks "
        f"and {counts['recording_meta']} recordings; "
        f"{counts['orphaned_paths']} tasks had a path their recording lacks"
    )


def downgrade() -> None:
    from vts.services.recording_backfill import refill_task_content

    op.add_column("tasks", sa.Column("transcript_path", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("summary_path", sa.Text(), nullable=True))
    counts = refill_task_content(op.get_bind())
    print(
        f"task content: refilled {counts['tasks']} tasks and "
        f"{counts['recordings']} recordings from their recordings"
    )
