"""GET /api/delivery-adapters — what the UI needs to build its forms (vts-j2kh).

The browser cannot import adapters, so the shape of their settings has to be
served. Without this the delivery UI cannot know which fields to render, nor
which of them belong to the shared connection rather than the destination.
"""
from __future__ import annotations

import pytest

from vts.delivery import registry
from vts.delivery.contract import DeliveryResult

SECRET_VALUE = "must-never-be-served"


class FakeAdapter:
    name = "fake"
    contract_version = (1, 1)

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "collection_id": {"type": "string"},
                "default_variant": {"type": "string",
                                    "enum": ["raw", "redacted", "summary"]},
            },
            "required": ["base_url", "collection_id"],
        }

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


@pytest.mark.asyncio
async def test_lists_installed_adapter_with_its_schema(client, monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", {"fake": FakeAdapter()}, raising=False)
    monkeypatch.setattr(registry, "_INCOMPATIBLE", {}, raising=False)

    resp = await client.get("/api/delivery-adapters")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [a["name"] for a in body["adapters"]] == ["fake"]
    adapter = body["adapters"][0]
    assert adapter["secret_keys"] == ["api_token"]
    assert adapter["connection_fields"] == ["base_url", "api_token"]
    # The schema is passed through verbatim; the UI renders fields from it.
    assert adapter["config_schema"]["required"] == ["base_url", "collection_id"]
    assert adapter["config_schema"]["properties"]["default_variant"]["enum"] == [
        "raw", "redacted", "summary"
    ]


@pytest.mark.asyncio
async def test_empty_when_no_plugins_are_installed(client, monkeypatch):
    """A base image with no plugins is a normal state, not an error."""
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)
    monkeypatch.setattr(registry, "_INCOMPATIBLE", {}, raising=False)

    resp = await client.get("/api/delivery-adapters")
    assert resp.status_code == 200
    assert resp.json() == {"adapters": [], "incompatible": {}}


@pytest.mark.asyncio
async def test_incompatible_adapters_are_reported_with_their_reason(client, monkeypatch):
    """Refused plugins must be visible, not silently absent — otherwise a
    target whose adapter stopped loading looks broken for no stated reason."""
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)
    monkeypatch.setattr(
        registry, "_INCOMPATIBLE",
        {"outline": "needs contract major 2, core provides 1"},
        raising=False,
    )

    resp = await client.get("/api/delivery-adapters")
    assert resp.status_code == 200
    assert resp.json()["incompatible"] == {
        "outline": "needs contract major 2, core provides 1"
    }


@pytest.mark.asyncio
async def test_one_broken_adapter_does_not_hide_the_others(client, monkeypatch):
    """config_schema() is third-party code. If it throws, the rest of the list
    must still be served, matching how the registry isolates load failures."""
    class Broken(FakeAdapter):
        name = "broken"

        def config_schema(self) -> dict:
            raise RuntimeError("plugin bug")

    monkeypatch.setattr(
        registry, "_CACHE",
        {"broken": Broken(), "fake": FakeAdapter()},
        raising=False,
    )
    monkeypatch.setattr(registry, "_INCOMPATIBLE", {}, raising=False)

    resp = await client.get("/api/delivery-adapters")
    assert resp.status_code == 200, resp.text
    assert [a["name"] for a in resp.json()["adapters"]] == ["fake"]


@pytest.mark.asyncio
async def test_never_serves_secret_values(client, monkeypatch):
    """Only the NAMES of secret keys are described here, never any value."""
    class WithSecrets(FakeAdapter):
        def config_schema(self) -> dict:
            return {"type": "object",
                    "properties": {"api_token": {"default": SECRET_VALUE}}}

    monkeypatch.setattr(registry, "_CACHE", {"fake": WithSecrets()}, raising=False)
    monkeypatch.setattr(registry, "_INCOMPATIBLE", {}, raising=False)

    resp = await client.get("/api/delivery-adapters")
    # The schema is the plugin's own declaration, but a stored secret can
    # never reach here: values live encrypted on the credential row.
    assert resp.status_code == 200
    creds = await client.get("/api/delivery-credentials")
    assert SECRET_VALUE not in creds.text
