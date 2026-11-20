"""recordings.listed_at: when a recording entered the library (vts-lzad)

The library is ordered by this moment instead of by creation, so a recording
that gets its text later lands on top. Existing recordings with text are
listed at their creation time.

Revision ID: 0035_recording_listed_at
Revises: 0034_recording_content_backfill
"""
import sqlalchemy as sa
from alembic import op

revision = "0035_recording_listed_at"
down_revision = "0034_recording_content_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from vts.services.recording_backfill import backfill_listed_at

    op.add_column(
        "recordings", sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_recordings_user_listed", "recordings", ["user_id", "listed_at", "id"]
    )
    changed = backfill_listed_at(op.get_bind())
    print(f"recording listed_at: set on {changed} recordings")


def downgrade() -> None:
    op.drop_index("ix_recordings_user_listed", table_name="recordings")
    op.drop_column("recordings", "listed_at")
