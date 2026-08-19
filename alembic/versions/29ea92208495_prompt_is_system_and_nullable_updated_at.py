"""prompt is_system and nullable updated_at

Revision ID: 29ea92208495
Revises: 0023_delivery_variant_width
Create Date: 2026-08-19 23:27:30.938529
"""
from alembic import op
import sqlalchemy as sa



revision = '29ea92208495'
down_revision = '0023_delivery_variant_width'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompts",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("prompts", "is_system", server_default=None)
    # Existing rows keep their timestamps: they are user prompts, and their
    # updated_at means what it always meant.
    op.alter_column("prompts", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.create_index(
        "ix_prompts_one_system_per_user",
        "prompts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_system"),
    )


def downgrade() -> None:
    op.drop_index("ix_prompts_one_system_per_user", table_name="prompts")
    op.execute("UPDATE prompts SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column("prompts", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_column("prompts", "is_system")

