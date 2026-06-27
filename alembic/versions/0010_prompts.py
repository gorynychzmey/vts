"""Add prompts table for user-defined custom prompts (VOS-63)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_prompts"
down_revision = "0009_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_prompts_user_created", "prompts", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_prompts_user_created", table_name="prompts")
    op.drop_table("prompts")
