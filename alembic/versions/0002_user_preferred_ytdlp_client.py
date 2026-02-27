"""Add per-user preferred yt-dlp player client."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_user_preferred_ytdlp_client"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_ytdlp_client", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "preferred_ytdlp_client")

