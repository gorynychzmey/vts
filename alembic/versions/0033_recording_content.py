"""recordings hold their own content fields (vts-lzad)

Adds `redacted_path` and `prompt_results` to `recordings`. Schema only: the
existing rows keep their data in `meta["prompt_results"]` until the content
backfill moves it.

Revision ID: 0033_recording_content
Revises: 0032_recording_state
"""
import sqlalchemy as sa
from alembic import op

revision = "0033_recording_content"
down_revision = "0032_recording_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recordings", sa.Column("redacted_path", sa.Text(), nullable=True))
    op.add_column(
        "recordings",
        sa.Column(
            "prompt_results", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")
        ),
    )


def downgrade() -> None:
    op.drop_column("recordings", "prompt_results")
    op.drop_column("recordings", "redacted_path")
