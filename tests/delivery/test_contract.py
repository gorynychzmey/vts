from datetime import datetime, timezone
from vts.delivery.contract import (
    TaskMeta, DeliveryPayload, DeliveryTargetConfig, DeliveryResult, DeliveryError,
)


def test_payload_is_frozen():
    meta = TaskMeta(source_url="u", source_title="t", language="en",
                    duration_s=1.0, created_at=datetime.now(timezone.utc))
    p = DeliveryPayload(task_id="x", variant="summary", content="c",
                        content_format="markdown", task=meta)
    assert p.variant == "summary"
    assert p.task.source_url == "u"


def test_result_defaults_none():
    r = DeliveryResult()
    assert r.external_id is None and r.external_url is None


def test_delivery_error_is_exception():
    assert issubclass(DeliveryError, Exception)
