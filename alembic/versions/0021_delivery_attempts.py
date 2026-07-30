"""delivery_attempts

Revision ID: 0021_delivery_attempts
Revises: 0020_delivery_targets
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0021_delivery_attempts"
down_revision = "0020_delivery_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True),
                  sa.ForeignKey("delivery_targets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delivery_attempts_status_next", "delivery_attempts",
                    ["status", "next_attempt_at"])
    op.create_index("ix_delivery_attempts_task", "delivery_attempts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_task", table_name="delivery_attempts")
    op.drop_index("ix_delivery_attempts_status_next", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
