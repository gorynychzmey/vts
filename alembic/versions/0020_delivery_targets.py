"""delivery_targets

Revision ID: 0020_delivery_targets
Revises: 0019_match_decision_is_noise
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0020_delivery_targets"
down_revision = "0019_match_decision_is_noise"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secrets_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_delivery_targets_user_name"),
    )
    op.create_index("ix_delivery_targets_user", "delivery_targets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_targets_user", table_name="delivery_targets")
    op.drop_table("delivery_targets")
