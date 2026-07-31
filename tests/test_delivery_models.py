from vts.db.models import DeliveryStatus, DeliveryTarget, DeliveryAttempt


def test_delivery_status_values():
    # No "failed": a failing delivery returns to "pending" (retry) or becomes
    # "dead" (attempts exhausted), so nothing ever set it. "waiting_adapter"
    # parks a delivery whose plugin is not loaded — transient, not a failure.
    assert {s.value for s in DeliveryStatus} == {
        "pending", "delivering", "delivered", "dead", "waiting_adapter"}


def test_target_columns_exist():
    cols = set(DeliveryTarget.__table__.columns.keys())
    assert {"id", "user_id", "name", "adapter", "config_json",
            "secrets_enc", "created_at", "updated_at"} <= cols


def test_attempt_columns_exist():
    cols = set(DeliveryAttempt.__table__.columns.keys())
    assert {"id", "task_id", "target_id", "adapter", "variant", "status",
            "attempts", "max_attempts", "next_attempt_at", "last_error",
            "external_id", "external_url"} <= cols
