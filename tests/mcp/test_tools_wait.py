from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask, FakeRedisWithPubSub
from vts.db.models import TaskStatus
from vts.mcp.tools import wait_for_task


async def test_wait_returns_immediately_if_terminal() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.completed)
    repo.tasks[t.id] = t

    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=5,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
    assert res.status == "completed"


async def test_wait_unblocks_on_task_status_event() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.running)
    repo.tasks[t.id] = t

    async def publish_later():
        await asyncio.sleep(0.05)
        t.status = TaskStatus.completed
        await redis.publish("vts:events", {
            "user_id": user.id,
            "task_id": str(t.id),
            "event": "task_status",
            "data": {"status": "completed"},
        })

    asyncio.create_task(publish_later())
    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=2,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
    assert res.status == "completed"


async def test_wait_timeout_returns_reached_false() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.running)
    repo.tasks[t.id] = t

    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=1,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is False
    assert res.status == "running"


async def test_wait_filters_other_users_events() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    other = FakeUser(id=str(uuid.uuid4()), username="bob")
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.running)
    repo.tasks[t.id] = t

    async def publish_noise():
        await asyncio.sleep(0.05)
        await redis.publish("vts:events", {
            "user_id": other.id,
            "task_id": str(t.id),
            "event": "task_status",
            "data": {"status": "completed"},
        })

    asyncio.create_task(publish_noise())
    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=1,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is False


async def test_wait_for_transcript_until(tmp_path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.running)
    repo.tasks[t.id] = t

    async def publish_phase():
        await asyncio.sleep(0.05)
        # Mirror what the pipeline emits at vts/pipeline/processor.py:1005
        await redis.publish("vts:events", {
            "user_id": user.id,
            "task_id": str(t.id),
            "event": "phase",
            "data": {"phase": "merge_transcript", "status": "done"},
        })

    asyncio.create_task(publish_phase())
    res = await wait_for_task(
        task_id=t.id,
        until="transcript",
        timeout_seconds=2,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
