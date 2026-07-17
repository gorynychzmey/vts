"""Voice samples with pgvector embeddings (vts-80i)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0016_voice_samples"
down_revision = "0015_speakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("speaker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding", Vector(256), nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=False),
        sa.Column("audio", sa.LargeBinary(), nullable=False),
        sa.Column("audio_format", sa.String(length=32), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["speaker_id"], ["speakers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_voice_samples_speaker", "voice_samples", ["speaker_id"])


def downgrade() -> None:
    op.drop_index("ix_voice_samples_speaker", table_name="voice_samples")
    op.drop_table("voice_samples")
