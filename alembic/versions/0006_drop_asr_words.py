"""Drop asr_words table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_drop_asr_words"
down_revision = "0005_task_summary_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_asr_words_segment", table_name="asr_words")
    op.drop_index("ix_asr_words_task_start", table_name="asr_words")
    op.drop_table("asr_words")


def downgrade() -> None:
    op.create_table(
        "asr_words",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=False),
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
