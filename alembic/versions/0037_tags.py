"""tags on recordings (vts-qwcj)

Creates `tags` and `recording_tags` and drops `recordings.tags`, a JSON column
nothing ever wrote to or read (every row holds `[]`).

Downgrade recreates `recordings.tags` as JSON `'[]'` and drops both tables.

Revision ID: 0037_tags
Revises: 0036_drop_task_content_columns
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_tags"
down_revision = "0036_drop_task_content_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("name_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name_key", name="uq_tags_user_key"),
    )
    op.create_table(
        "recording_tags",
        sa.Column(
            "recording_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "tag_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True,
        ),
    )
    op.create_index("ix_recording_tags_tag", "recording_tags", ["tag_id"])
    op.drop_column("recordings", "tags")


def downgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.drop_index("ix_recording_tags_tag", table_name="recording_tags")
    op.drop_table("recording_tags")
    op.drop_table("tags")
