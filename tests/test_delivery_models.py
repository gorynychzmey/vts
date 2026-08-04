from vts.db.models import (
    DeliveryAttempt,
    DeliveryCredential,
    DeliveryStatus,
    DeliveryTarget,
)


def test_delivery_status_values():
    # No "failed": a failing delivery returns to "pending" (retry) or becomes
    # "dead" (attempts exhausted), so nothing ever set it. "waiting_adapter"
    # parks a delivery whose plugin is not loaded — transient, not a failure.
    assert {s.value for s in DeliveryStatus} == {
        "pending", "delivering", "delivered", "dead", "waiting_adapter"}


def test_credential_columns_exist():
    cols = set(DeliveryCredential.__table__.columns.keys())
    assert {"id", "user_id", "name", "adapter", "config_json",
            "secrets_enc", "created_at", "updated_at"} <= cols


def test_target_columns_exist():
    cols = set(DeliveryTarget.__table__.columns.keys())
    assert {"id", "user_id", "name", "adapter", "config_json",
            "credential_id", "created_at", "updated_at"} <= cols


def test_target_no_longer_carries_secrets():
    """Secrets belong to the credential now (vts-929).

    Asserted explicitly rather than left implicit: a target row that still
    had its own secrets_enc would mean two places to rotate a token, which is
    the whole problem this split removes.
    """
    assert "secrets_enc" not in DeliveryTarget.__table__.columns.keys()


def test_target_credential_is_mandatory_and_restricts_delete():
    """The reference is required, and a credential in use cannot be deleted.

    RESTRICT rather than SET NULL: nulling it would leave a target that can
    never be delivered, silently, at the moment the credential goes away.
    """
    col = DeliveryTarget.__table__.columns["credential_id"]
    assert col.nullable is False
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "delivery_credentials"
    assert fk.ondelete == "RESTRICT"


def test_attempt_columns_exist():
    cols = set(DeliveryAttempt.__table__.columns.keys())
    assert {"id", "task_id", "target_id", "adapter", "variant", "status",
            "attempts", "max_attempts", "next_attempt_at", "last_error",
            "external_id", "external_url"} <= cols
