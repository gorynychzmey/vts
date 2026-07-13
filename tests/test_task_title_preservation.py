"""A user rename must survive pipeline-side title discovery.

Scenario (vts-hd7): the user renames a task while it is still queued
(PATCH /api/tasks/{id} writes source_title). When the worker later runs
step_download, the yt-dlp captured title is saved via
_save_task_source_title — it must NOT clobber the user's name. The same
rule applies to _clone_from_donor copying donor.source_title.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.pipeline.processor import TaskProcessor

from _db import make_test_engine


@pytest_asyncio.fixture
async def session_factory():
    """Postgres-backed sessionmaker (drop+recreate schema around each test)."""
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


async def _make_task(factory, source_title: str | None) -> uuid.UUID:
    async with factory() as session:
        user = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
        session.add(user)
        await session.flush()
        task = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            source_url="https://example.com/video",
            source_title=source_title,
            options={},
            artifact_dir="/tmp/task",
            status=TaskStatus.queued,
        )
        session.add(task)
        await session.commit()
        return task.id


async def _get_title(factory, task_id: uuid.UUID) -> str | None:
    async with factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task.source_title


def _processor(factory) -> TaskProcessor:
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.session_factory = factory
    return proc


@pytest.mark.asyncio
async def test_captured_title_fills_untitled_task(session_factory):
    task_id = await _make_task(session_factory, source_title=None)
    proc = _processor(session_factory)

    await proc._save_task_source_title(task_id, "yt-dlp video title")

    assert await _get_title(session_factory, task_id) == "yt-dlp video title"


@pytest.mark.asyncio
async def test_captured_title_does_not_clobber_user_rename(session_factory):
    # Renamed while queued → the user's name must survive execution.
    task_id = await _make_task(session_factory, source_title="Моё имя задачи")
    proc = _processor(session_factory)

    await proc._save_task_source_title(task_id, "yt-dlp video title")

    assert await _get_title(session_factory, task_id) == "Моё имя задачи"
