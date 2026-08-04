"""delivery_credentials: split the connection out of delivery_targets (vts-929)

Every target used to carry its own endpoint and secrets, so several
destinations on the same server duplicated both. The connection now lives in
delivery_credentials and each target references one.

Existing rows are migrated by giving every target its own credential holding
that target's whole config and secrets. That is deliberately NOT deduplicated:
the core cannot tell which config keys are connection fields — only the
adapter's connection_fields() knows, and adapters are not importable from a
migration. Over-copying is safe and lossless; the operator can merge
credentials afterwards. Production currently has no targets at all, so in
practice this loop does nothing.

Revision ID: 0022_delivery_credentials
Revises: 0021_delivery_attempts
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0022_delivery_credentials"
down_revision = "0021_delivery_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secrets_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_delivery_credentials_user_name"),
    )
    op.create_index("ix_delivery_credentials_user", "delivery_credentials", ["user_id"])

    # Nullable first: existing rows have no credential yet.
    op.add_column(
        "delivery_targets",
        sa.Column("credential_id", UUID(as_uuid=True), nullable=True),
    )

    # One credential per existing target, carrying its config and secrets.
    # `name` is unique per user, so deriving it from the target's own unique
    # name cannot collide.
    op.execute(
        """
        INSERT INTO delivery_credentials
            (id, user_id, name, adapter, config_json, secrets_enc,
             created_at, updated_at)
        SELECT gen_random_uuid(), t.user_id, t.name, t.adapter,
               t.config_json, t.secrets_enc, t.created_at, t.updated_at
        FROM delivery_targets t
        """
    )
    op.execute(
        """
        UPDATE delivery_targets t
        SET credential_id = c.id
        FROM delivery_credentials c
        WHERE c.user_id = t.user_id AND c.name = t.name
        """
    )

    op.alter_column("delivery_targets", "credential_id", nullable=False)
    op.create_foreign_key(
        "fk_delivery_targets_credential",
        "delivery_targets", "delivery_credentials",
        ["credential_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(
        "ix_delivery_targets_credential", "delivery_targets", ["credential_id"]
    )

    op.drop_column("delivery_targets", "secrets_enc")


def downgrade() -> None:
    op.add_column(
        "delivery_targets",
        sa.Column("secrets_enc", sa.LargeBinary(), nullable=True),
    )
    # Fold the connection's secrets back onto each target before the link goes.
    op.execute(
        """
        UPDATE delivery_targets t
        SET secrets_enc = c.secrets_enc
        FROM delivery_credentials c
        WHERE c.id = t.credential_id
        """
    )
    op.drop_index("ix_delivery_targets_credential", table_name="delivery_targets")
    op.drop_constraint(
        "fk_delivery_targets_credential", "delivery_targets", type_="foreignkey"
    )
    op.drop_column("delivery_targets", "credential_id")
    op.drop_index("ix_delivery_credentials_user", table_name="delivery_credentials")
    op.drop_table("delivery_credentials")
