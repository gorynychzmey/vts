"""The shared summary-restart reset: what it does, and in which order.

One helper backs both the API endpoint and the worker's reap hook. Before it
existed the two carried a line-by-line copy each and had already drifted on the
one thing that matters here — whether the irreversible file deletion happens
before or after the commit (vts-gouq).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from _db import make_test_engine
from vts.db.base import Base
from vts.db.models import Step, StepStatus, Task, TaskStatus
from vts.db.repo import Repo
from vts.services.summary_restart import reset_task_for_summary_restart


@pytest_asyncio.fixture
async def factory():
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(factory, art: Path) -> uuid.UUID:
    (art / "summary").mkdir(parents=True)
    (art / "summary" / "final.md").write_text("old summary")
    (art / "summary" / "window_1.txt").write_text("old window")
    async with factory() as s:
        repo = Repo(s)
        user = await repo.get_or_create_user("reset@example.com")
        await s.flush()
        task = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            source_url="u",
            status=TaskStatus.completed,
            artifact_dir=str(art),
            summary_path=str(art / "summary" / "final.md"),
            options={"prompts": [{"source": "system", "id": "summary"}]},
        )
        s.add(task)
        await s.flush()
        for name in ("download", "summarize_windows", "summarize_final"):
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        return task.id


async def _get_with_steps(session: AsyncSession, task_id: uuid.UUID) -> Task:
    """Load a task the way both real callers do.

    `steps` must be eager-loaded: _reset_summary_steps walks the relation, and
    lazy-loading it later raises MissingGreenlet under the async session. The
    endpoint gets this via Repo.get_task_for_user; the worker asks for it
    explicitly.
    """
    return await session.get(Task, task_id, options=[selectinload(Task.steps)])


@pytest.mark.asyncio
async def test_reset_requeues_the_task_and_clears_its_artefacts(factory, tmp_path):
    """The happy path: steps pending, status queued, summary files gone."""
    art = tmp_path / "task"
    task_id = await _seed(factory, art)

    async with factory() as s:
        task = await _get_with_steps(s, task_id)
        await reset_task_for_summary_restart(Repo(s), task)

    async with factory() as s:
        row = await _get_with_steps(s, task_id)
        assert row.status == TaskStatus.queued
        assert row.summary_path is None
        by_name = {st.name: st.status for st in row.steps}
        assert by_name["summarize_final"] == StepStatus.pending
        assert by_name["summarize_windows"] == StepStatus.pending
        assert by_name["download"] == StepStatus.completed, "head steps stay done"

    assert not (art / "summary" / "final.md").exists()
    assert not (art / "summary" / "window_1.txt").exists()


@pytest.mark.asyncio
async def test_a_failed_commit_leaves_the_files_intact(factory, tmp_path, monkeypatch):
    """I3: the irreversible half must never run before the recoverable one.

    The old order deleted the artefacts and *then* committed. A commit that
    failed — a dropped Postgres connection, a deadlock, a worker restart —
    rolled the DB back to `completed` with `summary_path` still pointing at a
    file that had already been unlinked, so the UI 404'd on a task that claimed
    to have a summary. Same defect class as vts-b6l.

    Committing first inverts the worst case into a self-healing one: `queued`
    plus stale files that the next run overwrites. This test forces the commit
    to fail and asserts the files survived.

    Reverting the fix (deleting before the commit) makes this fail: the
    artefacts are gone while the row is still `completed`.
    """
    art = tmp_path / "task"
    task_id = await _seed(factory, art)

    async with factory() as s:
        task = await _get_with_steps(s, task_id)
        repo = Repo(s)

        async def _boom() -> None:
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr(s, "commit", _boom)

        with pytest.raises(RuntimeError, match="connection reset"):
            await reset_task_for_summary_restart(repo, task)

    # The commit is the point of no return, and it did not happen — so nothing
    # irreversible may have happened either.
    assert (art / "summary" / "final.md").exists(), (
        "files were deleted before the commit that failed: the task is still "
        "'completed' but its summary is gone and the UI will 404"
    )
    assert (art / "summary" / "window_1.txt").exists()

    async with factory() as s:
        row = await s.get(Task, task_id)
        assert row.status == TaskStatus.completed
        assert row.summary_path == str(art / "summary" / "final.md")
