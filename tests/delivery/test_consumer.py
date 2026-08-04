from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.core.config import get_settings
from vts.db.base import Base
from vts.db.models import DeliveryStatus, Task, TaskStatus, User
from vts.db.repo import Repo
from vts.delivery import registry
from vts.delivery.consumer import delivery_tick
from vts.delivery.contract import DeliveryError, DeliveryResult


@pytest_asyncio.fixture
async def session_factory():
    """Postgres-backed sessionmaker (drop+recreate schema around each test).

    The consumer runs its own sessions internally, so the test drives it
    through the SAME factory it uses to seed/assert — a single shared engine.
    """
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class OkAdapter:
    name = "ok"

    def config_schema(self) -> dict:
        return {}

    def secret_keys(self) -> list[str]:
        return []

    def connection_fields(self) -> list[str]:
        return []

    async def deliver(self, payload, target):
        return DeliveryResult(external_id="doc9", external_url="http://o/doc9")


class BoomAdapter:
    name = "boom"

    def config_schema(self) -> dict:
        return {}

    def secret_keys(self) -> list[str]:
        return []

    def connection_fields(self) -> list[str]:
        return []

    async def deliver(self, payload, target):
        raise DeliveryError("nope")


async def _completed_task_with_raw(factory, tmp_path, adapter_name: str) -> Task:
    p = tmp_path / "transcript.txt"
    p.write_text("body", encoding="utf-8")
    async with factory() as session:
        repo = Repo(session)
        u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
        session.add(u)
        await session.flush()
        t = Task(
            id=uuid.uuid4(),
            user_id=u.id,
            source_url="http://x",
            options={},
            artifact_dir=str(tmp_path),
            transcript_path=str(p),
            status=TaskStatus.completed,
        )
        session.add(t)
        await session.flush()
        await repo.create_delivery_attempt(
            task_id=t.id,
            target_id=None,
            adapter=adapter_name,
            variant="raw",
            max_attempts=2,
            next_attempt_at=datetime.now(timezone.utc),
        )
        await session.commit()
        return t


@pytest.mark.asyncio
async def test_tick_delivers_success(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", {"ok": OkAdapter()}, raising=False)
    t = await _completed_task_with_raw(session_factory, tmp_path, "ok")
    await delivery_tick(session_factory, get_settings(), datetime.now(timezone.utc))
    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/doc9"


@pytest.mark.asyncio
async def test_tick_failure_retries_then_dead(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", {"boom": BoomAdapter()}, raising=False)
    settings = get_settings()
    t = await _completed_task_with_raw(session_factory, tmp_path, "boom")
    now = datetime.now(timezone.utc)

    # attempt 1 → retryable (attempts=1 < max_attempts=2), so back to pending
    await delivery_tick(session_factory, settings, now)
    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.pending

    # force it due again; attempt 2 reaches max_attempts=2 → dead
    async with session_factory() as session:
        repo = Repo(session)
        await repo.record_delivery_failure(
            rows[0].id, last_error="x", next_attempt_at=now, dead=False
        )
        await session.commit()
    await delivery_tick(session_factory, settings, now)
    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.dead


@pytest.mark.asyncio
async def test_missing_adapter_parks_without_spending_attempts(
    session_factory, tmp_path, monkeypatch
):
    """A plugin that did not load is transient, not a failure (spec cce964c)."""
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # nothing registered
    settings = get_settings()
    t = await _completed_task_with_raw(session_factory, tmp_path, "ok")
    now = datetime.now(timezone.utc)

    # max_attempts=2, so two ticks would kill a genuinely failing delivery.
    await delivery_tick(session_factory, settings, now)
    await delivery_tick(session_factory, settings, now)

    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.waiting_adapter
    assert rows[0].attempts == 0, "a missing adapter must not spend attempts"
    assert rows[0].next_attempt_at is not None


@pytest.mark.asyncio
async def test_parked_delivery_leaves_once_adapter_returns(
    session_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)
    settings = get_settings()
    t = await _completed_task_with_raw(session_factory, tmp_path, "ok")
    now = datetime.now(timezone.utc)

    await delivery_tick(session_factory, settings, now)
    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.waiting_adapter

    # Plugin comes back; the parked row is due (wait interval elapsed) and delivers.
    monkeypatch.setattr(registry, "_CACHE", {"ok": OkAdapter()}, raising=False)
    later = now + timedelta(seconds=settings.delivery_adapter_wait_seconds + 1)
    await delivery_tick(session_factory, settings, later)

    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/doc9"
