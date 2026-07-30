from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from vts.db.models import Task, TaskStatus

from tests.conftest import _TEST_USER_ID


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER_ID = uuid.UUID(_TEST_USER_ID)


class _FakeRedis:
    """Minimal async Redis stub: enough for the queue-position cache used
    by GET /api/tasks."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    """Attach a FakeRedis to app.state so get_redis() works without a real Redis."""
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


async def _seed(factory, n):
    """Insert n tasks for the test user; task i at BASE + i minutes."""
    rows = []
    async with factory() as s:
        for i in range(n):
            t = Task(
                id=uuid.uuid4(),
                user_id=USER_ID,
                source_url=f"https://example.com/{i}",
                status=TaskStatus.completed,
                created_at=BASE + timedelta(minutes=i),
                artifact_dir="/tmp/x",
            )
            s.add(t)
            rows.append(t)
        await s.commit()
    return rows


@pytest_asyncio.fixture
def factory(authed_app):
    _app, factory = authed_app
    return factory


async def test_limit_returns_newest_first(client, factory):
    await _seed(factory, 5)
    r = await client.get("/api/tasks?limit=2")
    assert r.status_code == 200
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/4", "https://example.com/3"]


async def test_before_cursor_pages_down(client, factory):
    rows = await _seed(factory, 5)
    tail = rows[3]
    r = await client.get(
        "/api/tasks",
        params={
            "limit": 2,
            "before_ts": tail.created_at.isoformat(),
            "before_id": str(tail.id),
        },
    )
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/2", "https://example.com/1"]


async def test_after_cursor_order_asc(client, factory):
    rows = await _seed(factory, 5)
    head = rows[1]
    r = await client.get(
        "/api/tasks",
        params={
            "limit": 2,
            "order": "asc",
            "after_ts": head.created_at.isoformat(),
            "after_id": str(head.id),
        },
    )
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/2", "https://example.com/3"]


async def test_half_cursor_pair_is_422(client):
    r = await client.get(
        "/api/tasks", params={"before_ts": "2026-01-01T00:00:00+00:00"}
    )
    assert r.status_code == 422


async def test_after_ge_before_is_422(client, factory):
    rows = await _seed(factory, 3)
    older, newer = rows[0], rows[2]  # before must be strictly newer than after
    r = await client.get(
        "/api/tasks",
        params={
            "after_ts": newer.created_at.isoformat(),
            "after_id": str(newer.id),
            "before_ts": older.created_at.isoformat(),
            "before_id": str(older.id),
        },
    )
    assert r.status_code == 422


async def test_bad_order_is_422(client):
    r = await client.get("/api/tasks?order=sideways")
    assert r.status_code == 422


async def test_compact_legacy_path(client, factory):
    await _seed(factory, 3)
    r = await client.get("/api/tasks", params={"compact": "true", "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    for item in body:
        assert "steps" not in item
        assert "options" not in item


async def test_compact_cursor_path(client, factory):
    rows = await _seed(factory, 3)
    tail = rows[2]
    r = await client.get(
        "/api/tasks",
        params={
            "compact": "true",
            "limit": 2,
            "before_ts": tail.created_at.isoformat(),
            "before_id": str(tail.id),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    for item in body:
        assert "steps" not in item
        assert "options" not in item


async def test_created_at_is_fixed_width_and_lexicographically_ordered(client, factory):
    """Pins the VOS-84 fix: pydantic's default datetime serialization omits
    fractional seconds when microsecond==0 (e.g. "...56Z") but includes them
    otherwise (e.g. "...56.000001Z"). Since "Z" (0x5A) sorts after "."
    (0x2E), two same-second timestamps could compare as *out of order* as
    plain strings — which breaks both the client's isNewerThan() comparison
    and any future string-based cursor comparison. TaskOut/TaskCompactOut
    force isoformat(timespec="microseconds") so created_at is always
    fixed-width and lexicographic order matches chronological order.
    """
    same_second = datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=timezone.utc)
    async with factory() as s:
        older = Task(
            id=uuid.uuid4(),
            user_id=USER_ID,
            source_url="https://example.com/zero-us",
            status=TaskStatus.completed,
            created_at=same_second,  # microsecond == 0
            artifact_dir="/tmp/x",
        )
        newer = Task(
            id=uuid.uuid4(),
            user_id=USER_ID,
            source_url="https://example.com/nonzero-us",
            status=TaskStatus.completed,
            created_at=same_second + timedelta(microseconds=1),
            artifact_dir="/tmp/x",
        )
        s.add(older)
        s.add(newer)
        await s.commit()

    r = await client.get("/api/tasks?limit=2")
    assert r.status_code == 200
    by_url = {t["source_url"]: t["created_at"] for t in r.json()}
    zero_us_str = by_url["https://example.com/zero-us"]
    nonzero_us_str = by_url["https://example.com/nonzero-us"]

    # Fixed-width: both must carry exactly 6 fractional digits.
    assert zero_us_str.endswith(".000000+00:00"), zero_us_str
    assert nonzero_us_str.endswith(".000001+00:00"), nonzero_us_str
    assert len(zero_us_str) == len(nonzero_us_str)

    # Lexicographic order must match chronological order (older < newer).
    assert zero_us_str < nonzero_us_str

    # Also pin the compact path, which uses the same serializer.
    rc = await client.get("/api/tasks", params={"compact": "true", "limit": 2})
    assert rc.status_code == 200
    by_url_compact = {t["source_url"]: t["created_at"] for t in rc.json()}
    assert by_url_compact["https://example.com/zero-us"] < by_url_compact["https://example.com/nonzero-us"]
