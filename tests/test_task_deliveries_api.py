from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from vts.db.models import DeliveryStatus, Task, TaskStatus, User
from vts.db.repo import Repo

# Must match tests/conftest.py::_TEST_USER_ID — the user the authed client acts as.
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class _FakeRedis:
    """The app's redis normally comes from the lifespan handler, which the
    ASGITransport-based test client never runs. Retry only publishes a wake-up
    hint, so recording the calls is enough."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


@pytest.fixture
def fake_redis(authed_app):
    app, _factory = authed_app
    fake = _FakeRedis()
    app.state.redis = fake
    return fake


async def _task_with_attempt(factory, *, user_id=TEST_USER_ID, adapter="fake", **attempt_kw):
    """Create a completed task owned by `user_id` plus one delivery attempt."""
    async with factory() as session:
        repo = Repo(session)
        task = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url="http://x",
            options={},
            artifact_dir="/tmp/x",
            status=TaskStatus.completed,
        )
        session.add(task)
        await session.flush()
        attempt = await repo.create_delivery_attempt(
            task_id=task.id,
            target_id=None,
            adapter=adapter,
            variant="raw",
            max_attempts=attempt_kw.pop("max_attempts", 2),
            next_attempt_at=datetime.now(timezone.utc),
        )
        await session.commit()
        return task, attempt


@pytest.mark.asyncio
async def test_lists_deliveries_for_owned_task(client, authed_app):
    _app, factory = authed_app
    task, _attempt = await _task_with_attempt(factory)

    resp = await client.get(f"/api/tasks/{task.id}/deliveries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["adapter"] == "fake"
    assert body[0]["variant"] == "raw"
    assert body[0]["status"] == "pending"
    assert body[0]["waiting_for_adapter"] is False


@pytest.mark.asyncio
async def test_waiting_adapter_is_flagged_not_an_error(client, authed_app):
    """UI must be able to say "waiting for plugin" rather than "failed"."""
    _app, factory = authed_app
    task, attempt = await _task_with_attempt(factory)
    async with factory() as session:
        repo = Repo(session)
        await repo.park_delivery_for_adapter(
            attempt.id, next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=300)
        )
        await session.commit()

    body = (await client.get(f"/api/tasks/{task.id}/deliveries")).json()
    assert body[0]["status"] == DeliveryStatus.waiting_adapter.value
    assert body[0]["waiting_for_adapter"] is True
    assert body[0]["last_error"] is None, "a missing plugin is not an error"


@pytest.mark.asyncio
async def test_retry_revives_dead_delivery(client, authed_app, fake_redis):
    _app, factory = authed_app
    task, attempt = await _task_with_attempt(factory)
    async with factory() as session:
        repo = Repo(session)
        await repo.record_delivery_failure(
            attempt.id, last_error="boom", next_attempt_at=None, dead=True
        )
        await session.commit()

    assert (await client.get(f"/api/tasks/{task.id}/deliveries")).json()[0]["status"] == "dead"

    resp = await client.post(f"/api/tasks/{task.id}/deliveries/retry", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reset": 1}
    assert (await client.get(f"/api/tasks/{task.id}/deliveries")).json()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_retry_leaves_parked_rows_alone(client, authed_app, fake_redis):
    """waiting_adapter is not stuck; forcing it to pending would burn attempts."""
    _app, factory = authed_app
    task, attempt = await _task_with_attempt(factory)
    async with factory() as session:
        repo = Repo(session)
        await repo.park_delivery_for_adapter(
            attempt.id, next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=300)
        )
        await session.commit()

    resp = await client.post(f"/api/tasks/{task.id}/deliveries/retry", json={})
    assert resp.json() == {"reset": 0}
    body = (await client.get(f"/api/tasks/{task.id}/deliveries")).json()
    assert body[0]["status"] == DeliveryStatus.waiting_adapter.value


@pytest.mark.asyncio
async def test_other_users_task_is_404(client, authed_app, fake_redis):
    _app, factory = authed_app
    async with factory() as session:
        stranger = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
        session.add(stranger)
        await session.flush()
        stranger_id = stranger.id
        await session.commit()

    task, _ = await _task_with_attempt(factory, user_id=stranger_id)

    assert (await client.get(f"/api/tasks/{task.id}/deliveries")).status_code == 404
    assert (
        await client.post(f"/api/tasks/{task.id}/deliveries/retry", json={})
    ).status_code == 404


@pytest.mark.asyncio
async def test_unknown_task_is_404(client):
    missing = uuid.uuid4()
    assert (await client.get(f"/api/tasks/{missing}/deliveries")).status_code == 404
