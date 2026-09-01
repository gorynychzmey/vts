"""browser sessions live in the database, not Redis (vts-akf8)

The `{sid -> email}` record behind every logged-in browser lived only in Redis.
Redis is a cache and a bus in this deployment, not storage: production runs it
with `appendonly no` (RDB `save 60 1` only) and shares the instance with other
tenants, so its persistence cannot be tightened for our sake alone. A hard
restart therefore logged every user out — measured 2026-09-01, with eight live
sessions in the keyspace.

The row stores a SHA-256 of the sid rather than the sid itself, the same way
`api_tokens` stores `token_hash`: the sid is a bearer credential, and unlike a
Redis key with a TTL a database row is dumped, backed up and kept.

`expires_at` replaces what the TTL used to do. Nothing removes a row when its
time is up, so the lookup filters on it explicitly and a sweep reclaims the
space; the filter is what decides whether a session is live, never the sweep.

No data is migrated here. The live sessions are moved by
`scripts/migrate_sessions_to_db.py`, run against production before the deploy
while the sids are still readable in Redis — after this change they exist only
as hashes and could not be reconstructed.

Revision ID: 0030_user_sessions
Revises: 0029_recording_title_custom
"""
import sqlalchemy as sa
from alembic import op

revision = "0030_user_sessions"
down_revision = "0029_recording_title_custom"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("sid_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.BigInteger, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sid_hash", name="uq_user_sessions_sid_hash"),
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_user", "user_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_sessions_user", table_name="user_sessions")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_table("user_sessions")
