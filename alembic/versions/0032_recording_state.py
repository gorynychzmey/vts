"""recordings carry their own completeness state (vts-lzad)

A recording used to borrow its meaning from the task: an `archived` task did
not say whether the run finished. `recordings.state` (processing / complete /
incomplete) records it on the recording itself. Existing rows are backfilled
from their task and files by vts.services.recording_state.

Revision ID: 0032_recording_state
Revises: 0031_source_timeline
"""
import sqlalchemy as sa
from alembic import op

revision = "0032_recording_state"
down_revision = "0031_source_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from vts.services.recording_state import backfill_recording_states

    op.add_column(
        "recordings",
        sa.Column("state", sa.String(length=10), nullable=False, server_default="processing"),
    )
    n = backfill_recording_states(op.get_bind())
    print(f"recording state: backfilled {n} recordings")


def downgrade() -> None:
    op.drop_column("recordings", "state")
