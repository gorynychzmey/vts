"""widen delivery_attempts.variant for prompt refs (vts-as1i)

`variant` used to hold one of three words, so String(32) was ample. It can now
also hold a prompt ref like "user:<uuid>" — 41 characters — which would fail
the insert at the moment a delivery is enqueued, i.e. after the task had
already run.

Widening only; no data is rewritten and every existing value still fits.

Revision ID: 0023_delivery_variant_width
Revises: 0022_delivery_credentials
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_delivery_variant_width"
down_revision = "0022_delivery_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "delivery_attempts", "variant",
        existing_type=sa.String(32), type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Rows carrying a prompt ref do not fit in 32 chars. Drop them rather than
    # letting Postgres abort the whole downgrade: they are delivery ATTEMPTS
    # (retryable bookkeeping), not user data, and the feature that created
    # them does not exist below this revision.
    op.execute("DELETE FROM delivery_attempts WHERE length(variant) > 32")
    op.alter_column(
        "delivery_attempts", "variant",
        existing_type=sa.String(64), type_=sa.String(32),
        existing_nullable=False,
    )
