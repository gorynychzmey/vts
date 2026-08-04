"""Submitting a delivery that targets a prompt result (vts-as1i).

The rule under test: you may deliver a prompt's output, but only if that
prompt is actually selected for the task. Otherwise the delivery is queued
against an artifact nothing will ever produce, and the mistake only surfaces
much later, at delivery time.
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


async def _target(client):
    cred = await client.post(
        "/api/delivery-credentials",
        json={"name": "conn", "adapter": "fake",
              "config": {"base_url": "https://o.example"},
              "secrets": {"api_token": "t"}},
        headers=_HEADERS,
    )
    target = await client.post(
        "/api/delivery-targets",
        json={"name": "dest", "adapter": "fake",
              "credential_id": cred.json()["id"], "config": {}},
        headers=_HEADERS,
    )
    return target.json()["id"]


async def _prompt(client, name="Memo"):
    resp = await client.post(
        "/api/prompts",
        json={"name": name, "system_prompt": "Summarise the action items."},
        headers=_HEADERS,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_delivering_a_selected_prompt_result_is_accepted(client):
    target_id = await _target(client)
    prompt_id = await _prompt(client)

    resp = await client.post(
        "/api/tasks",
        json={
            "url": "https://example.com/v",
            "prompts": [{"source": "user", "id": prompt_id}],
            "delivery": [{"deliver_to": target_id, "variant": f"user:{prompt_id}"}],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"]["delivery"] == [
        {"deliver_to": target_id, "variant": f"user:{prompt_id}"}
    ]


@pytest.mark.asyncio
async def test_delivering_an_unselected_prompt_result_is_rejected(client):
    """The whole point of validating at submit: without this the task runs,
    the delivery is enqueued, and only then discovers there is no artifact."""
    target_id = await _target(client)
    prompt_id = await _prompt(client)

    resp = await client.post(
        "/api/tasks",
        json={
            "url": "https://example.com/v",
            # A different prompt is selected, not the one being delivered.
            "prompts": [{"source": "system", "id": "summary"}],
            "delivery": [{"deliver_to": target_id, "variant": f"user:{prompt_id}"}],
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert prompt_id in resp.text


@pytest.mark.asyncio
async def test_malformed_variant_is_rejected(client):
    target_id = await _target(client)
    resp = await client.post(
        "/api/tasks",
        json={"url": "https://example.com/v",
              "delivery": [{"deliver_to": target_id, "variant": "bogus:"}]},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_fixed_variants_are_unaffected(client):
    target_id = await _target(client)
    resp = await client.post(
        "/api/tasks",
        json={"url": "https://example.com/v",
              "delivery": [{"deliver_to": target_id, "variant": "raw"}]},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_upload_path_accepts_a_prompt_variant(client):
    """The upload flows carry delivery as JSON, so they need the same
    extension — otherwise the feature would exist for URLs only."""
    target_id = await _target(client)
    prompt_id = await _prompt(client)

    resp = await client.post(
        "/api/uploads/init",
        json={
            "filename": "clip.mp4", "total_size": 3,
            "prompts": json.dumps([{"source": "user", "id": prompt_id}]),
            "delivery": json.dumps(
                [{"deliver_to": target_id, "variant": f"user:{prompt_id}"}]
            ),
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_upload_path_rejects_a_malformed_prompt_variant(client):
    target_id = await _target(client)
    resp = await client.post(
        "/api/uploads/init",
        json={"filename": "clip.mp4", "total_size": 3,
              "delivery": json.dumps([{"deliver_to": target_id, "variant": "bad:"}])},
        headers=_HEADERS,
    )
    assert resp.status_code == 422
    assert "variant" in resp.text


@pytest.mark.asyncio
async def test_attempt_row_holds_a_full_prompt_ref(client, authed_app):
    """A prompt ref is 41 characters and the column used to be String(32),
    so this is the write that would previously have failed (migration 0023)."""
    from sqlalchemy import select

    from vts.db.models import DeliveryAttempt, Task, TaskStatus
    from vts.db.repo import Repo

    _app, factory = authed_app
    target_id = await _target(client)
    prompt_id = await _prompt(client)
    ref = f"user:{prompt_id}"
    assert len(ref) > 32, "this test is meaningless if the ref is short"

    async with factory() as session:
        repo = Repo(session)
        task = (await session.scalars(select(Task))).first()
        if task is None:
            created = await client.post(
                "/api/tasks",
                json={"url": "https://example.com/v",
                      "prompts": [{"source": "user", "id": prompt_id}],
                      "delivery": [{"deliver_to": target_id, "variant": ref}]},
                headers=_HEADERS,
            )
            assert created.status_code == 200, created.text
            task = (await session.scalars(select(Task))).first()

        await repo.create_delivery_attempt(
            task_id=task.id, target_id=uuid.UUID(target_id), adapter="fake",
            variant=ref, max_attempts=5, next_attempt_at=None,
        )
        await session.commit()

        stored = (await session.scalars(
            select(DeliveryAttempt).where(DeliveryAttempt.variant == ref)
        )).first()
        assert stored is not None, "a full-length prompt ref must round-trip"
        assert stored.variant == ref
