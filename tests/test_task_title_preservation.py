"""A user rename must survive pipeline-side title discovery.

Scenario (vts-hd7): the user renames a task while it is still queued
(PATCH /api/tasks/{id} writes source_title). When the worker later runs
DownloadStep.run, the yt-dlp captured title is saved via
ctx.save_task_source_title — it must NOT clobber the user's name. The same
rule applies to _clone_from_donor copying donor.source_title.

These tests drive the real DownloadStep.run end-to-end over a DB-backed
context, stubbing only the yt-dlp download so it emits a media title via
progress_cb. The step's title capture plus the real save/clobber rule are
both exercised.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo
from vts.pipeline.steps.base import StepState
from vts.pipeline.steps.media import DownloadStep

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


class _DownloadCtx:
    """Minimal PipelineContext surface DownloadStep.run touches, with the
    title-save wired to the real DB-backed persistence (clobber rule included)."""

    def __init__(self, factory, source_url: str) -> None:
        self.session_factory = factory
        self._source_url = source_url
        self.settings = SimpleNamespace(
            ytdlp_cookies_file=None,
            ytdlp_cookies_from_browser=None,
            ytdlp_youtube_player_client=None,
            ytdlp_youtube_po_token=None,
            ytdlp_verbose=False,
        )
        self.bus = SimpleNamespace(publish_event=self._publish_event)

    async def _publish_event(self, **kwargs) -> None:
        return None

    def task_flag(self, options, key, *, default: bool) -> bool:
        value = options.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    async def task_url(self, task_id: uuid.UUID) -> str:
        return self._source_url

    async def get_user_preferred_ytdlp_client(self, user_id: uuid.UUID):
        return None

    async def set_user_preferred_ytdlp_client(self, user_id, player_client) -> None:
        return None

    async def save_task_source_title(self, task_id: uuid.UUID, title: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            if task.source_title:
                return
            task.source_title = title
            await session.commit()


async def _run_download_step(factory, task_id: uuid.UUID, user_id: uuid.UUID, tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)

    def _fake_download(*, source_url, media_dir, progress_cb, phase_cb, logger, audio_only, **kwargs):
        # yt-dlp reports the discovered media title through progress_cb.
        progress_cb("download", {"media_title": "yt-dlp video title"})
        (Path(media_dir) / "audio.original.m4a").write_bytes(b"a")
        return (None, None, None)

    ctx = _DownloadCtx(factory, "https://example.com/video")
    st = StepState(
        task_id=task_id,
        user_id=str(user_id),
        dirs={"media": media, "outputs": tmp_path / "out", "segments": tmp_path / "seg"},
        logger=logging.getLogger("test.download_title"),
        task_options={"audio_only": True},
    )

    import vts.pipeline.steps.media as media_mod

    orig = media_mod.download_video_and_audio
    media_mod.download_video_and_audio = _fake_download
    try:
        await DownloadStep().run(ctx, st)
    finally:
        media_mod.download_video_and_audio = orig
    # Let the loop.call_soon_threadsafe media_progress task drain.
    await asyncio.sleep(0)


async def _task_user(factory, task_id):
    async with factory() as session:
        task = await session.get(Task, task_id)
        return task.user_id


@pytest.mark.asyncio
async def test_captured_title_fills_untitled_task(session_factory, tmp_path):
    task_id = await _make_task(session_factory, source_title=None)
    user_id = await _task_user(session_factory, task_id)

    await _run_download_step(session_factory, task_id, user_id, tmp_path)

    assert await _get_title(session_factory, task_id) == "yt-dlp video title"


@pytest.mark.asyncio
async def test_captured_title_does_not_clobber_user_rename(session_factory, tmp_path):
    # Renamed while queued → the user's name must survive execution.
    task_id = await _make_task(session_factory, source_title="Моё имя задачи")
    user_id = await _task_user(session_factory, task_id)

    await _run_download_step(session_factory, task_id, user_id, tmp_path)

    assert await _get_title(session_factory, task_id) == "Моё имя задачи"
