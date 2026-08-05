"""Connection checks and field option lists (vts-6o37, contract 1.2).

Both endpoints reach into an external system with a user's stored credentials,
so the tests care about three things beyond the happy path: an adapter built
against 1.1 must keep working, one user must not be able to probe another's
system, and a dead external system must not make the form unusable.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from vts.core.config import get_settings
from vts.delivery import registry
from vts.delivery.contract import CheckOutcome, CheckResult, ConfigOption, DeliveryResult

_HEADERS = {"X-Forwarded-User": "tester"}


class LegacyAdapter:
    """A contract-1.1 adapter: no check_connection, no config_options."""

    name = "legacy"
    contract_version = (1, 1)

    def config_schema(self) -> dict:
        return {"type": "object", "properties": {"base_url": {"type": "string"}}}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


class ModernAdapter(LegacyAdapter):
    """A 1.2 adapter implementing both optional methods."""

    name = "modern"
    contract_version = (1, 2)

    def option_fields(self) -> list[str]:
        return ["collection_id"]

    async def check_connection(self, target):
        # Echoes whatever the stored config asks for, so a test can pick the
        # outcome it wants to exercise.
        wanted = (target.config or {}).get("_outcome", "ok")
        if wanted == "boom":
            raise RuntimeError("adapter blew up")
        return CheckResult(CheckOutcome(wanted), detail=(target.config or {}).get("_detail"))

    async def config_options(self, field, target):
        if (target.config or {}).get("_options") == "down":
            raise RuntimeError("Outline is unreachable")
        if field != "collection_id":
            return []
        return [ConfigOption(value="c-1", label="Meetings"),
                ConfigOption(value="c-2", label="Notes")]


@pytest.fixture(autouse=True)
def _adapters_and_key(monkeypatch):
    monkeypatch.setattr(
        registry, "_CACHE",
        {"legacy": LegacyAdapter(), "modern": ModernAdapter()},
        raising=False,
    )
    monkeypatch.setattr(registry, "_INCOMPATIBLE", {}, raising=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_key", Fernet.generate_key().decode(), raising=False)
    yield


async def _credential(client, *, adapter="modern", config=None, name=None):
    resp = await client.post(
        "/api/delivery-credentials",
        json={"name": name or f"c-{uuid.uuid4().hex[:6]}", "adapter": adapter,
              "config": config or {"base_url": "https://o.example"},
              "secrets": {"api_token": "tok"}},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# --- capability advertising -------------------------------------------------


@pytest.mark.asyncio
async def test_adapters_report_what_they_support(client):
    """The UI needs to know whether to offer the check button and a picker,
    without probing every adapter."""
    body = (await client.get("/api/delivery-adapters", headers=_HEADERS)).json()
    by_name = {a["name"]: a for a in body["adapters"]}

    assert by_name["modern"]["supports_check"] is True
    assert by_name["modern"]["option_fields"] == ["collection_id"]
    # A 1.1 adapter advertises neither, and is still listed.
    assert by_name["legacy"]["supports_check"] is False
    assert by_name["legacy"]["option_fields"] == []


# --- checking a connection --------------------------------------------------


@pytest.mark.asyncio
async def test_successful_check(client):
    cid = await _credential(client)
    resp = await client.post(f"/api/delivery-credentials/{cid}/check", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "outcome": "ok", "detail": None}


@pytest.mark.parametrize(
    "outcome", ["unreachable", "unauthorized", "not_found", "unexpected_response", "timeout"]
)
@pytest.mark.asyncio
async def test_failure_outcomes_are_reported_as_codes(client, outcome):
    """A code, not prose: the adapter knows what broke, the UI knows how to
    say it in the user's language. Prose from the plugin would scatter i18n
    across every adapter."""
    cid = await _credential(client, config={"base_url": "u", "_outcome": outcome})
    body = (await client.post(f"/api/delivery-credentials/{cid}/check", headers=_HEADERS)).json()
    assert body["ok"] is False
    assert body["outcome"] == outcome


@pytest.mark.asyncio
async def test_detail_is_passed_through(client):
    cid = await _credential(
        client, config={"base_url": "u", "_outcome": "unauthorized", "_detail": "HTTP 401"}
    )
    body = (await client.post(f"/api/delivery-credentials/{cid}/check", headers=_HEADERS)).json()
    assert body["detail"] == "HTTP 401"


@pytest.mark.asyncio
async def test_adapter_that_raises_does_not_500(client):
    """A plugin raising instead of reporting is a plugin bug — but the user
    still gets an answer rather than a server error."""
    cid = await _credential(client, config={"base_url": "u", "_outcome": "boom"})
    resp = await client.post(f"/api/delivery-credentials/{cid}/check", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False and body["outcome"] == "error"
    assert "blew up" in body["detail"]


@pytest.mark.asyncio
async def test_legacy_adapter_reports_unsupported_not_broken(client):
    """Contract 1.1 adapters have no check_connection. That is a missing
    capability, not a failure — and must never look like a broken connection."""
    cid = await _credential(client, adapter="legacy")
    resp = await client.post(f"/api/delivery-credentials/{cid}/check", headers=_HEADERS)
    assert resp.status_code == 501, resp.text


# --- listing field options --------------------------------------------------


@pytest.mark.asyncio
async def test_options_carry_separate_value_and_label(client):
    """Show the name, store the id: an Outline collection can be renamed, so
    binding a target to the name would break it."""
    cid = await _credential(client)
    body = (await client.get(
        f"/api/delivery-credentials/{cid}/options/collection_id", headers=_HEADERS
    )).json()
    assert body["options"] == [
        {"value": "c-1", "label": "Meetings"},
        {"value": "c-2", "label": "Notes"},
    ]
    assert body["unavailable"] is None


@pytest.mark.asyncio
async def test_unreachable_system_degrades_instead_of_blocking(client):
    """The external system being down must not make the target form unusable
    — the UI falls back to typing the id by hand."""
    cid = await _credential(client, config={"base_url": "u", "_options": "down"})
    resp = await client.get(
        f"/api/delivery-credentials/{cid}/options/collection_id", headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["options"] == []
    assert "unreachable" in body["unavailable"]


@pytest.mark.asyncio
async def test_legacy_adapter_reports_unsupported_options(client):
    cid = await _credential(client, adapter="legacy")
    resp = await client.get(
        f"/api/delivery-credentials/{cid}/options/collection_id", headers=_HEADERS
    )
    assert resp.status_code == 501


# --- ownership --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_check_another_users_credential(client, authed_app):
    """Both endpoints reach an external system with stored secrets. Without an
    ownership check, a credential id would be enough to probe someone else's
    Outline — and to learn whether their token still works."""
    from vts.db.models import DeliveryCredential, User

    _app, factory = authed_app
    stranger_cred = uuid.uuid4()
    async with factory() as session:
        other = User(id=uuid.uuid4(), username=f"other-{uuid.uuid4().hex[:6]}")
        session.add(other)
        await session.flush()
        session.add(DeliveryCredential(
            id=stranger_cred, user_id=other.id, name="theirs",
            adapter="modern", config_json={"base_url": "https://theirs.example"},
        ))
        await session.commit()

    check = await client.post(
        f"/api/delivery-credentials/{stranger_cred}/check", headers=_HEADERS
    )
    assert check.status_code == 404, "must not reveal another user's credential"

    options = await client.get(
        f"/api/delivery-credentials/{stranger_cred}/options/collection_id", headers=_HEADERS
    )
    assert options.status_code == 404


@pytest.mark.asyncio
async def test_unknown_credential_is_404(client):
    missing = uuid.uuid4()
    assert (await client.post(
        f"/api/delivery-credentials/{missing}/check", headers=_HEADERS
    )).status_code == 404
