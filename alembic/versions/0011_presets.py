"""Add presets table and users.default_preset (vts-hp7)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_presets"
down_revision = "0010_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_presets_user_created", "presets", ["user_id", "created_at"])
    op.add_column("users", sa.Column("default_preset", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "default_preset")
    op.drop_index("ix_presets_user_created", table_name="presets")
    op.drop_table("presets")
