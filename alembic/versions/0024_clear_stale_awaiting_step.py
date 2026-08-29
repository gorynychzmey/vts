"""clear awaiting_step left behind on tasks that are no longer waiting (vts-47w6)

`awaiting_step` was written by set_awaiting_input and cleared by nobody, so a
task that resumed and finished kept naming the step it had once paused at. The
API then served the contradictory pair `status=completed,
awaiting_step=match_speakers` (observed on a production task).

The code no longer creates such rows — set_task_status clears the field — but
rows already written stay wrong until someone rewrites them. This does that
once.

Data-only: no schema change. Nothing is lost — the step is a scratch field
describing an in-flight wait, and every row touched here is one whose wait has
already ended.

Revision ID: 0024_clear_stale_awaiting_step
Revises: 29ea92208495
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_clear_stale_awaiting_step"
down_revision = "29ea92208495"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only rows that are NOT waiting: a task genuinely parked in
    # awaiting_input must keep the step, otherwise the resolve dialog loses
    # the thing it dispatches on.
    op.execute(
        sa.text(
            "UPDATE tasks SET awaiting_step = NULL "
            "WHERE awaiting_step IS NOT NULL AND status <> 'awaiting_input'"
        )
    )


def downgrade() -> None:
    # The previous values described waits that had already ended; there is
    # nothing meaningful to restore, and inventing a step would recreate the
    # very contradiction this removed.
    pass
