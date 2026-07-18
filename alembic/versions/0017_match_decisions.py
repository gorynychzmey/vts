"""Match decisions — record every human match/reject/override for calibration (vts-80i)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_match_decisions"
down_revision = "0016_voice_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("speaker_label", sa.String(), nullable=False),
        sa.Column("speaker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("voice_sample_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("distance", sa.Float(), nullable=True),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["speaker_id"], ["speakers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voice_sample_id"], ["voice_samples.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_match_decisions_user", "match_decisions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_match_decisions_user", table_name="match_decisions")
    op.drop_table("match_decisions")
