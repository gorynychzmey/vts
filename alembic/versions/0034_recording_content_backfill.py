"""every task has its recording, recordings hold their content (vts-lzad)

Creates a recording for each task that has none, and fills `redacted_path`
and `prompt_results` on recordings that still have their task, from the files
on disk. See vts/services/recording_backfill.py.

The files live on the data volume, so the content half needs it mounted where
migrations run, as the pod's migrate container has it. Where it is not, only
the recordings are created and a notice is printed.

Revision ID: 0034_recording_content_backfill
Revises: 0033_recording_content
"""
from alembic import op

revision = "0034_recording_content_backfill"
down_revision = "0033_recording_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from vts.core.config import get_settings
    from vts.services.recording_backfill import (
        artifacts_available,
        backfill_recording_content,
    )

    conn = op.get_bind()
    counts = backfill_recording_content(
        conn,
        artifacts_available=artifacts_available(conn, get_settings().artifacts_root),
    )
    print(
        f"recording content: created {counts['created']} recordings, "
        f"set redacted_path on {counts['redacted']}, "
        f"prompt_results on {counts['prompt_results']}"
    )


def downgrade() -> None:
    # Refused rather than a silent no-op. Nothing marks a row as written by
    # this migration: the recordings it created and the fields it filled look
    # exactly like what the live code writes, so a downgrade cannot tell which
    # to undo.
    raise RuntimeError(
        "0034_recording_content_backfill cannot be downgraded: the recordings "
        "and content fields it wrote are indistinguishable from ones written "
        "by the application. Restore a database backup instead."
    )
