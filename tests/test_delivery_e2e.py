"""End-to-end: a completed task's delivery goes from options to the adapter.

Proves the pieces built across Tasks 1-13 actually compose: preset/submit
options -> enqueue on completion -> durable row -> consumer claim -> variant
resolved from artifacts -> secrets decrypted -> adapter called -> status
recorded. Uses a fake adapter; no external service is contacted.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.core.config import get_settings
from vts.core.secrets import encrypt_secrets
from vts.db.base import Base
from vts.db.models import DeliveryStatus, Task, TaskStatus, User
from vts.db.repo import Repo
from vts.delivery import registry
from vts.delivery.consumer import delivery_tick
from vts.delivery.contract import DeliveryResult
from vts.delivery.queue import enqueue_deliveries


class RecordingAdapter:
    name = "rec"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str]] = []

    def config_schema(self) -> dict:
        return {}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    async def deliver(self, payload, target):
        self.calls.append(
            (payload.variant, target.secrets.get("api_token"), payload.content)
        )
        return DeliveryResult(external_id="e1", external_url="http://o/e1")


@pytest_asyncio.fixture
async def session_factory():
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


@pytest.mark.asyncio
async def test_enqueue_then_consume_delivers_with_decrypted_secret(
    session_factory, tmp_path, monkeypatch
):
    settings = get_settings()
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "secrets_key", key, raising=False)
    rec = RecordingAdapter()
    monkeypatch.setattr(registry, "_CACHE", {"rec": rec}, raising=False)

    transcript = tmp_path / "transcript.txt"
    transcript.write_text("hello from the transcript", encoding="utf-8")

    async with session_factory() as session:
        repo = Repo(session)
        user = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
        session.add(user)
        await session.flush()
        await repo.create_delivery_target(
            user.id,
            name="out",
            adapter="rec",
            config={"default_variant": "raw"},
            secrets_enc=encrypt_secrets({"api_token": "tok"}, key),
        )
        task = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            source_url="http://x",
            options={"delivery": [{"deliver_to": "out"}]},
            artifact_dir=str(tmp_path),
            transcript_path=str(transcript),
            status=TaskStatus.completed,
        )
        session.add(task)
        await session.flush()

        enqueued = await enqueue_deliveries(
            repo, task, max_attempts=3, now=datetime.now(timezone.utc)
        )
        await session.commit()
        task_id = task.id

    assert enqueued == 1, "completion must enqueue one delivery per configured target"

    await delivery_tick(session_factory, settings, datetime.now(timezone.utc))

    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(task_id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/e1"
    # variant came from the target default; the secret reached the adapter
    # decrypted; the content is the artifact's real text.
    assert rec.calls == [("raw", "tok", "hello from the transcript")]


@pytest.mark.asyncio
async def test_delivery_survives_the_plugin_being_absent_then_returning(
    session_factory, tmp_path, monkeypatch
):
    """The scenario the plugin loader makes routine: a restart without the plugin.

    The delivery must not die and must not need human intervention.
    """
    settings = get_settings()
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # no plugins loaded

    transcript = tmp_path / "transcript.txt"
    transcript.write_text("body", encoding="utf-8")

    async with session_factory() as session:
        repo = Repo(session)
        user = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
        session.add(user)
        await session.flush()
        await repo.create_delivery_target(
            user.id, name="out", adapter="rec", config={"default_variant": "raw"},
            secrets_enc=None,
        )
        task = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            source_url="http://x",
            options={"delivery": [{"deliver_to": "out"}]},
            artifact_dir=str(tmp_path),
            transcript_path=str(transcript),
            status=TaskStatus.completed,
        )
        session.add(task)
        await session.flush()
        await enqueue_deliveries(repo, task, max_attempts=2, now=datetime.now(timezone.utc))
        await session.commit()
        task_id = task.id

    now = datetime.now(timezone.utc)
    # More ticks than max_attempts: a real failure would be dead by now.
    await delivery_tick(session_factory, settings, now)
    await delivery_tick(session_factory, settings, now)
    await delivery_tick(session_factory, settings, now)

    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(task_id)
    assert rows[0].status == DeliveryStatus.waiting_adapter
    assert rows[0].attempts == 0
    assert rows[0].external_url is None

    # The plugin is installed and the worker restarts.
    rec = RecordingAdapter()
    monkeypatch.setattr(registry, "_CACHE", {"rec": rec}, raising=False)
    later = now + timedelta(seconds=settings.delivery_adapter_wait_seconds + 1)
    await delivery_tick(session_factory, settings, later)

    async with session_factory() as session:
        rows = await Repo(session).list_deliveries_for_task(task_id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rec.calls == [("raw", None, "body")]
