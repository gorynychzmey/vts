"""Delivery on the UPLOAD paths, not just the URL path (vts-j2kh).

/api/tasks has accepted `delivery` since the delivery core landed, but the
three upload flows (form upload, chunked single-file, chunked multi-file) did
not. The new-task card is one form that switches between a URL and a file, so
without this a destination picked for an uploaded file would be silently
dropped — the user would see it accepted and nothing would ever be delivered.
"""
from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from vts.core.config import get_settings
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult

_HEADERS = {"X-Forwarded-User": "tester"}


class FakeAdapter:
    name = "fake"
    contract_version = (1, 1)

    def config_schema(self) -> dict:
        return {"type": "object"}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


@pytest.fixture(autouse=True)
def _adapter_and_key(monkeypatch, tmp_path):
    """Point artifacts at a tmp dir AND register the fake adapter.

    Both live in one fixture deliberately: get_settings() is cached, so the
    env var has to be set before the first call that builds Settings. Split
    across two fixtures, whichever ran first would freeze the default
    artifacts root and the upload would try to write to /srv/vts-data.
    """
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    monkeypatch.setattr(registry, "_CACHE", {"fake": FakeAdapter()}, raising=False)
    settings = get_settings()
    monkeypatch.setattr(
        settings, "secrets_key", Fernet.generate_key().decode(), raising=False
    )
    yield


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def publish(self, channel, message) -> int:
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


async def _target(client, name="dest"):
    cred = await client.post(
        "/api/delivery-credentials",
        json={"name": f"conn-{name}", "adapter": "fake",
              "config": {"base_url": "https://o.example"},
              "secrets": {"api_token": "t"}},
        headers=_HEADERS,
    )
    assert cred.status_code == 200, cred.text
    target = await client.post(
        "/api/delivery-targets",
        json={"name": name, "adapter": "fake",
              "credential_id": cred.json()["id"], "config": {}},
        headers=_HEADERS,
    )
    assert target.status_code == 200, target.text
    return target.json()["id"]


@pytest.mark.asyncio
async def test_form_upload_stores_delivery(client):
    target_id = await _target(client)
    resp = await client.post(
        "/api/tasks/upload",
        files={"file": ("clip.mp4", b"abc", "video/mp4")},
        data={"delivery": json.dumps([{"deliver_to": target_id, "variant": "raw"}])},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"]["delivery"] == [
        {"deliver_to": target_id, "variant": "raw"}
    ]


@pytest.mark.asyncio
async def test_chunked_single_file_upload_stores_delivery(client):
    target_id = await _target(client)
    init = await client.post(
        "/api/uploads/init",
        json={"filename": "clip.mp4", "total_size": 3,
              "delivery": json.dumps([{"deliver_to": target_id}])},
        headers=_HEADERS,
    )
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]
    await client.patch(f"/api/uploads/{upload_id}?offset=0", content=b"abc", headers=_HEADERS)

    fin = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert fin.status_code == 200, fin.text
    assert fin.json()["options"]["delivery"] == [{"deliver_to": target_id}]


@pytest.mark.asyncio
async def test_upload_rejects_unknown_target_at_init(client):
    """Validated when the form is submitted, not after the bytes are sent:
    a finalize-time failure would arrive after the whole upload."""
    missing = str(uuid.uuid4())
    resp = await client.post(
        "/api/uploads/init",
        json={"filename": "clip.mp4", "total_size": 3,
              "delivery": json.dumps([{"deliver_to": missing}])},
        headers=_HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert missing in resp.text


@pytest.mark.asyncio
async def test_form_upload_rejects_unknown_target(client):
    missing = str(uuid.uuid4())
    resp = await client.post(
        "/api/tasks/upload",
        files={"file": ("clip.mp4", b"abc", "video/mp4")},
        data={"delivery": json.dumps([{"deliver_to": missing}])},
        headers=_HEADERS,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_upload_rejects_malformed_delivery(client):
    resp = await client.post(
        "/api/uploads/init",
        json={"filename": "clip.mp4", "total_size": 3, "delivery": "not json"},
        headers=_HEADERS,
    )
    assert resp.status_code == 422
    assert "valid JSON" in resp.text


@pytest.mark.asyncio
async def test_upload_rejects_bad_variant(client):
    target_id = await _target(client)
    resp = await client.post(
        "/api/uploads/init",
        json={"filename": "clip.mp4", "total_size": 3,
              "delivery": json.dumps([{"deliver_to": target_id, "variant": "nope"}])},
        headers=_HEADERS,
    )
    assert resp.status_code == 422
    assert "variant" in resp.text


@pytest.mark.asyncio
async def test_upload_without_delivery_still_works(client):
    """The common case must be untouched: no delivery means an empty list,
    not a failure and not a missing key."""
    resp = await client.post(
        "/api/tasks/upload",
        files={"file": ("clip.mp4", b"abc", "video/mp4")},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"]["delivery"] == []
