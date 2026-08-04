from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from vts.core.config import get_settings
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult

SECRET_VALUE = "supersecret-token-value"
BASE_URL = "https://outline.example/api"


class FakeAdapter:
    name = "fake"
    contract_version = (1, 1)

    def config_schema(self) -> dict:
        # Mirrors the real Outline adapter: a connection field and a
        # per-destination field, both required once merged.
        return {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "collection_id": {"type": "string"},
            },
            "required": ["base_url", "collection_id"],
        }

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


@pytest.fixture(autouse=True)
def _fake_adapter_and_key(monkeypatch):
    """Register a fake adapter and a usable secrets key for every test here."""
    monkeypatch.setattr(registry, "_CACHE", {"fake": FakeAdapter()}, raising=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_key", Fernet.generate_key().decode(), raising=False)
    yield


async def _credential(client, *, name="conn", secrets=True):
    body = {"name": name, "adapter": "fake", "config": {"base_url": BASE_URL}}
    if secrets:
        body["secrets"] = {"api_token": SECRET_VALUE}
    resp = await client.post("/api/delivery-credentials", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _target(client, credential_id, *, name="t", collection="c1"):
    resp = await client.post(
        "/api/delivery-targets",
        json={"name": name, "adapter": "fake", "credential_id": credential_id,
              "config": {"collection_id": collection}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- credentials -----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_credential_returns_presence_marker_not_secret_value(client):
    body = await _credential(client, name="outline-main")
    assert body["name"] == "outline-main"
    assert body["adapter"] == "fake"
    assert body["config"] == {"base_url": BASE_URL}
    assert body["secrets"] == {"api_token": {"set": True}}
    assert body["adapter_available"] is True
    assert body["used_by"] == 0


@pytest.mark.asyncio
async def test_credential_list_and_get_never_leak_secret_values(client):
    created = await _credential(client, name="c1")

    listing = await client.get("/api/delivery-credentials")
    assert listing.status_code == 200
    assert SECRET_VALUE not in listing.text
    assert any(c["id"] == created["id"] for c in listing.json())

    detail = await client.get(f"/api/delivery-credentials/{created['id']}")
    assert detail.status_code == 200
    assert SECRET_VALUE not in detail.text
    assert detail.json()["secrets"] == {"api_token": {"set": True}}


@pytest.mark.asyncio
async def test_update_credential_without_secrets_keeps_the_stored_one(client):
    created = await _credential(client, name="keep")
    resp = await client.put(
        f"/api/delivery-credentials/{created['id']}",
        json={"config": {"base_url": "https://other.example/api"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"] == {"base_url": "https://other.example/api"}
    assert body["secrets"] == {"api_token": {"set": True}}, "secret must survive an update"
    assert SECRET_VALUE not in resp.text


@pytest.mark.asyncio
async def test_clear_credential_secrets_empties_presence(client):
    created = await _credential(client, name="clearme")
    resp = await client.put(
        f"/api/delivery-credentials/{created['id']}", json={"clear_secrets": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["secrets"] == {"api_token": {"set": False}}


@pytest.mark.asyncio
async def test_rotating_one_credential_serves_every_target_on_it(client):
    """The point of the split (vts-929): one endpoint, one token, many
    destinations. Before this, the token lived on each target and rotating it
    meant editing every one."""
    cred = await _credential(client, name="shared")
    a = await _target(client, cred["id"], name="meetings", collection="c1")
    b = await _target(client, cred["id"], name="notes", collection="c2")

    assert a["credential_id"] == b["credential_id"] == cred["id"]

    detail = (await client.get(f"/api/delivery-credentials/{cred['id']}")).json()
    assert detail["used_by"] == 2

    rotated = await client.put(
        f"/api/delivery-credentials/{cred['id']}",
        json={"secrets": {"api_token": "rotated-value"}},
    )
    assert rotated.status_code == 200, rotated.text
    assert "rotated-value" not in rotated.text


@pytest.mark.asyncio
async def test_credential_in_use_cannot_be_deleted(client):
    """RESTRICT surfaced as a 409 with a count, not as an integrity error."""
    cred = await _credential(client, name="busy")
    await _target(client, cred["id"], name="dependent")

    resp = await client.delete(f"/api/delivery-credentials/{cred['id']}")
    assert resp.status_code == 409, resp.text
    assert "1" in resp.text

    # Removing the dependent target frees it.
    targets = (await client.get("/api/delivery-targets")).json()
    tid = next(t["id"] for t in targets if t["name"] == "dependent")
    assert (await client.delete(f"/api/delivery-targets/{tid}")).status_code == 204
    assert (await client.delete(f"/api/delivery-credentials/{cred['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_unknown_adapter_rejected_on_credential_create(client):
    resp = await client.post(
        "/api/delivery-credentials",
        json={"name": "bad", "adapter": "does-not-exist", "config": {}},
    )
    assert resp.status_code == 400
    assert "does-not-exist" in resp.text


# --- targets ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_target_references_a_credential_and_holds_no_secrets(client):
    cred = await _credential(client)
    body = await _target(client, cred["id"], name="outline-meetings")

    assert body["credential_id"] == cred["id"]
    assert body["config"] == {"collection_id": "c1"}
    assert body["adapter_available"] is True
    assert "secrets" not in body, "secrets belong to the credential, not the target"
    assert SECRET_VALUE not in str(body)


@pytest.mark.asyncio
async def test_target_config_is_validated_against_the_MERGED_schema(client):
    """A target lacking a required CONNECTION field is still valid, because
    the credential supplies it; a target lacking its own required field is not.

    This is the case that makes validating the merge (rather than either half)
    necessary: `base_url` is required by the schema but never lives on a target.
    """
    cred = await _credential(client)

    ok = await client.post(
        "/api/delivery-targets",
        json={"name": "good", "adapter": "fake", "credential_id": cred["id"],
              "config": {"collection_id": "c1"}},
    )
    assert ok.status_code == 200, ok.text

    bad = await client.post(
        "/api/delivery-targets",
        json={"name": "bad", "adapter": "fake", "credential_id": cred["id"],
              "config": {}},
    )
    assert bad.status_code == 422, bad.text
    assert "collection_id" in bad.text


@pytest.mark.asyncio
async def test_target_rejects_a_credential_for_another_adapter(client, monkeypatch):
    """A credential is adapter-specific: an Outline token cannot authenticate
    an S3 target. Caught with a reason rather than left to fail at delivery."""
    class OtherAdapter(FakeAdapter):
        name = "other"

    monkeypatch.setattr(
        registry, "_CACHE",
        {"fake": FakeAdapter(), "other": OtherAdapter()},
        raising=False,
    )
    cred = await client.post(
        "/api/delivery-credentials",
        json={"name": "other-conn", "adapter": "other", "config": {"base_url": BASE_URL}},
    )
    assert cred.status_code == 200, cred.text

    resp = await client.post(
        "/api/delivery-targets",
        json={"name": "mismatched", "adapter": "fake",
              "credential_id": cred.json()["id"], "config": {"collection_id": "c1"}},
    )
    assert resp.status_code == 422, resp.text
    assert "other" in resp.text and "fake" in resp.text


@pytest.mark.asyncio
async def test_target_rejects_an_unknown_credential(client):
    missing = uuid.uuid4()
    resp = await client.post(
        "/api/delivery-targets",
        json={"name": "x", "adapter": "fake", "credential_id": str(missing),
              "config": {"collection_id": "c1"}},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_target_requires_a_credential(client):
    resp = await client.post(
        "/api/delivery-targets",
        json={"name": "x", "adapter": "fake", "config": {"collection_id": "c1"}},
    )
    assert resp.status_code == 422, "credential_id is mandatory"


@pytest.mark.asyncio
async def test_target_can_be_moved_to_another_credential(client):
    a = await _credential(client, name="conn-a")
    b = await _credential(client, name="conn-b")
    target = await _target(client, a["id"], name="movable")

    resp = await client.put(
        f"/api/delivery-targets/{target['id']}", json={"credential_id": b["id"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["credential_id"] == b["id"]


@pytest.mark.asyncio
async def test_target_survives_its_adapter_disappearing(client, monkeypatch):
    """Settings must outlive a plugin that failed to load (spec cce964c)."""
    cred = await _credential(client, name="orphan-conn")
    created = await _target(client, cred["id"], name="orphan")

    # The plugin is gone after a restart.
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)

    listing = await client.get("/api/delivery-targets")
    assert listing.status_code == 200, "listing must not fail when an adapter is missing"
    row = next(t for t in listing.json() if t["id"] == created["id"])
    assert row["adapter_available"] is False
    assert row["name"] == "orphan"
    assert row["config"] == {"collection_id": "c1"}

    creds = await client.get("/api/delivery-credentials")
    assert creds.status_code == 200
    assert SECRET_VALUE not in creds.text
    assert next(c["id"] == cred["id"] for c in creds.json())


@pytest.mark.asyncio
async def test_delete_and_404s(client):
    cred = await _credential(client, name="conn-del")
    created = await _target(client, cred["id"], name="gone")

    assert (await client.delete(f"/api/delivery-targets/{created['id']}")).status_code == 204
    assert (await client.get(f"/api/delivery-targets/{created['id']}")).status_code == 404
    assert (await client.delete(f"/api/delivery-targets/{created['id']}")).status_code == 404

    missing = uuid.uuid4()
    assert (await client.put(f"/api/delivery-targets/{missing}", json={"name": "x"})).status_code == 404
