"""Add user_step_weights table (vts-8cm)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_user_step_weights"
down_revision = "0011_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_step_weights",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("final_summary_fallback", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_counts", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_step_weights_user"),
    )
    op.create_index("ix_user_step_weights_user", "user_step_weights", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_step_weights_user", table_name="user_step_weights")
    op.drop_table("user_step_weights")
