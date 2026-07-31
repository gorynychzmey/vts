from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from vts.core.config import get_settings
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult

SECRET_VALUE = "supersecret-token-value"


class FakeAdapter:
    name = "fake"

    def config_schema(self) -> dict:
        return {"type": "object"}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


@pytest.fixture(autouse=True)
def _fake_adapter_and_key(monkeypatch):
    """Register a fake adapter and a usable secrets key for every test here."""
    monkeypatch.setattr(registry, "_CACHE", {"fake": FakeAdapter()}, raising=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_key", Fernet.generate_key().decode(), raising=False)
    yield


@pytest.mark.asyncio
async def test_create_returns_presence_marker_not_secret_value(client):
    resp = await client.post(
        "/api/delivery-targets",
        json={
            "name": "outline-meetings",
            "adapter": "fake",
            "config": {"collection_id": "c1"},
            "secrets": {"api_token": SECRET_VALUE},
        },
    )
    assert resp.status_code == 200, resp.text
    assert SECRET_VALUE not in resp.text, "secret value must never be returned"
    body = resp.json()
    assert body["name"] == "outline-meetings"
    assert body["adapter"] == "fake"
    assert body["config"] == {"collection_id": "c1"}
    assert body["secrets"] == {"api_token": {"set": True}}
    assert body["adapter_available"] is True


@pytest.mark.asyncio
async def test_list_and_get_never_leak_secret_values(client):
    created = (
        await client.post(
            "/api/delivery-targets",
            json={"name": "t1", "adapter": "fake", "config": {},
                  "secrets": {"api_token": SECRET_VALUE}},
        )
    ).json()

    listing = await client.get("/api/delivery-targets")
    assert listing.status_code == 200
    assert SECRET_VALUE not in listing.text
    assert any(t["id"] == created["id"] for t in listing.json())

    detail = await client.get(f"/api/delivery-targets/{created['id']}")
    assert detail.status_code == 200
    assert SECRET_VALUE not in detail.text
    assert detail.json()["secrets"] == {"api_token": {"set": True}}


@pytest.mark.asyncio
async def test_unknown_adapter_rejected_on_create(client):
    resp = await client.post(
        "/api/delivery-targets",
        json={"name": "bad", "adapter": "does-not-exist", "config": {}},
    )
    assert resp.status_code == 400
    assert "does-not-exist" in resp.text


@pytest.mark.asyncio
async def test_update_without_secrets_keeps_the_stored_one(client):
    created = (
        await client.post(
            "/api/delivery-targets",
            json={"name": "keep", "adapter": "fake", "config": {"a": 1},
                  "secrets": {"api_token": SECRET_VALUE}},
        )
    ).json()

    resp = await client.put(
        f"/api/delivery-targets/{created['id']}",
        json={"config": {"a": 2}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"] == {"a": 2}
    assert body["secrets"] == {"api_token": {"set": True}}, "secret must survive an update"
    assert SECRET_VALUE not in resp.text


@pytest.mark.asyncio
async def test_clear_secrets_empties_presence(client):
    created = (
        await client.post(
            "/api/delivery-targets",
            json={"name": "clearme", "adapter": "fake", "config": {},
                  "secrets": {"api_token": SECRET_VALUE}},
        )
    ).json()

    resp = await client.put(
        f"/api/delivery-targets/{created['id']}", json={"clear_secrets": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["secrets"] == {"api_token": {"set": False}}


@pytest.mark.asyncio
async def test_target_survives_its_adapter_disappearing(client, monkeypatch):
    """Settings must outlive a plugin that failed to load (spec cce964c)."""
    created = (
        await client.post(
            "/api/delivery-targets",
            json={"name": "orphan", "adapter": "fake", "config": {"x": 1},
                  "secrets": {"api_token": SECRET_VALUE}},
        )
    ).json()

    # The plugin is gone after a restart.
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)

    listing = await client.get("/api/delivery-targets")
    assert listing.status_code == 200, "listing must not fail when an adapter is missing"
    row = next(t for t in listing.json() if t["id"] == created["id"])
    assert row["adapter_available"] is False
    assert row["name"] == "orphan"
    assert row["config"] == {"x": 1}
    assert SECRET_VALUE not in listing.text

    detail = await client.get(f"/api/delivery-targets/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["adapter_available"] is False


@pytest.mark.asyncio
async def test_delete_and_404s(client):
    created = (
        await client.post(
            "/api/delivery-targets",
            json={"name": "gone", "adapter": "fake", "config": {}},
        )
    ).json()

    assert (await client.delete(f"/api/delivery-targets/{created['id']}")).status_code == 204
    assert (await client.get(f"/api/delivery-targets/{created['id']}")).status_code == 404
    assert (await client.delete(f"/api/delivery-targets/{created['id']}")).status_code == 404

    missing = uuid.uuid4()
    assert (await client.put(f"/api/delivery-targets/{missing}", json={"name": "x"})).status_code == 404
