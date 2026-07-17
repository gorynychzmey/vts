"""Enable pgvector."""
from __future__ import annotations

from alembic import op

revision = "0014_pgvector_extension"
down_revision = "0013_task_status_waiting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
