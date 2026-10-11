"""The task list must survive Redis being unreachable (prod incident 2026-10-11).

What happened: `GET /api/tasks?limit=10` answered 500 four times across two
users. The traceback ends in the queue-position CACHE read —

    tasks.py:317  list_tasks
    task_input.py:129  _get_cached_queue_positions
    redis.exceptions.ConnectionError: Error while reading from …:6379
        : (104, 'Connection reset by peer')

— and Redis itself was up throughout (PING 12 ms, 19 days uptime, `timeout 0`,
`rejected_connections 0`). A pooled connection had been reset in the network
path while idle, and the client, built with no `health_check_interval` and no
retry on `ConnectionError`, handed it out dead.

The point this pins is not the reset, which will happen again, but that an
OPTIONAL cache took a REQUIRED endpoint down with it. Queue positions and lane
positions are decorations on the list; the list is the product. The database
can answer `get_global_queue_positions()` without Redis at all, and
`_get_lane_positions` already degrades to `{}` on malformed JSON — just not on
a transport failure.

So: Redis unreachable means a list without positions, never a 500.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from vts.db.models import Task, TaskStatus

from tests.conftest import _TEST_USER_ID

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER_ID = uuid.UUID(_TEST_USER_ID)


class _DeadRedis:
    """Every command fails the way a reset pooled connection fails.

    Not a refusal to connect: the real incident had a connection that was
    established and then torn down underneath the client, which is why the
    error surfaces on read rather than at startup.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _die(self, name):
        self.calls.append(name)
        raise RedisConnectionError(
            "Error while reading from redis.example:6379 : (104, 'Connection reset by peer')"
        )

    async def get(self, *_a, **_k):
        self._die("get")

    async def setex(self, *_a, **_k):
        self._die("setex")

    async def publish(self, *_a, **_k):
        self._die("publish")


@pytest.fixture
def dead_redis(authed_app):
    app, _factory = authed_app
    dead = _DeadRedis()
    app.state.redis = dead
    return dead


@pytest_asyncio.fixture
def factory(authed_app):
    _app, factory = authed_app
    return factory


async def _seed(factory, n):
    async with factory() as session:
        for i in range(n):
            session.add(Task(
                id=uuid.uuid4(),
                user_id=USER_ID,
                source_url=f"https://example.com/{i}",
                status=TaskStatus.completed,
                created_at=BASE + timedelta(minutes=i),
                artifact_dir="/tmp/x",
            ))
        await session.commit()


async def test_task_list_is_served_while_redis_is_unreachable(client, factory, dead_redis):
    await _seed(factory, 3)

    response = await client.get("/api/tasks?limit=10")

    assert response.status_code == 200, response.text
    # EXACT count: the list is the product, and a degraded answer that quietly
    # drops rows would be worse than the 500 it replaces.
    assert len(response.json()) == 3
    # The cache really was consulted — otherwise this test would also pass on a
    # build where the helper had been removed instead of made tolerant.
    assert "get" in dead_redis.calls


async def test_a_single_task_is_served_while_redis_is_unreachable(client, factory, dead_redis):
    """The detail endpoint reads the same cache (tasks.py:130)."""
    await _seed(factory, 1)
    listed = await client.get("/api/tasks?limit=1")
    assert listed.status_code == 200, listed.text
    task_id = listed.json()[0]["id"]

    response = await client.get(f"/api/tasks/{task_id}")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == task_id


def test_the_shared_client_can_notice_a_connection_that_died_idle():
    """A tripwire on the construction, not on behaviour, and deliberately so.

    The reset itself cannot be provoked from a test: it happens in the network
    path, between two commands, outside the process. What a test CAN hold is
    that the client is built able to cope — probing an idle connection before
    reuse and retrying once when a command rides a dead one. Drop those kwargs
    and this fails instead of the next person's request.

    The tolerance above is what keeps the endpoint up; this only narrows how
    often it is needed.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    from vts.services.redis_client import make_redis

    kwargs = make_redis("redis://unused.example:6379/0").connection_pool.connection_kwargs

    assert kwargs.get("health_check_interval") == 30
    assert kwargs.get("socket_keepalive") is True
    assert RedisConnectionError in (kwargs.get("retry_on_error") or [])
    assert getattr(kwargs.get("retry"), "_retries", 0) >= 1
