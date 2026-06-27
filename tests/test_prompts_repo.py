from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vts.db.base import Base
from vts.db.models import Prompt, Task, TaskStatus, User
from vts.db.repo import Repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """In-memory SQLite async session for repo integration tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess

    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(session: AsyncSession) -> uuid.UUID:
    user = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(user)
    await session.flush()
    return user.id


async def _make_task(session: AsyncSession, user_id: uuid.UUID) -> Task:
    task = Task(
        id=uuid.uuid4(),
        user_id=user_id,
        source_url="https://example.com/video",
        options={},
        artifact_dir="/tmp/task",
        status=TaskStatus.queued,
    )
    session.add(task)
    await session.flush()
    return task


# ---------------------------------------------------------------------------
# Existing model column test
# ---------------------------------------------------------------------------


def test_prompt_model_columns():
    cols = set(Prompt.__table__.columns.keys())
    assert {"id", "user_id", "name", "system_prompt",
            "created_at", "updated_at"} <= cols


# ---------------------------------------------------------------------------
# Prompt CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_crud_roundtrip(session):
    repo = Repo(session)
    uid = await _make_user(session)

    created = await repo.create_prompt(uid, "My Prompt", "Do the thing.")
    assert created.name == "My Prompt"

    listed = await repo.list_prompts(uid)
    assert [p.id for p in listed] == [created.id]

    fetched = await repo.get_prompt(uid, created.id)
    assert fetched is not None and fetched.system_prompt == "Do the thing."

    updated = await repo.update_prompt(uid, created.id, name="Renamed", system_prompt=None)
    assert updated is not None and updated.name == "Renamed"
    assert updated.system_prompt == "Do the thing."

    assert await repo.delete_prompt(uid, created.id) is True
    assert await repo.get_prompt(uid, created.id) is None


@pytest.mark.asyncio
async def test_prompt_isolation_between_users(session):
    repo = Repo(session)
    uid_a = await _make_user(session)
    uid_b = await _make_user(session)
    p = await repo.create_prompt(uid_a, "A", "a")
    assert await repo.get_prompt(uid_b, p.id) is None
    assert await repo.delete_prompt(uid_b, p.id) is False


# ---------------------------------------------------------------------------
# set_task_prompt_results test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_task_prompt_results_roundtrip(session):
    repo = Repo(session)
    uid = await _make_user(session)
    task = await _make_task(session, uid)

    prompt_results = [
        {
            "source": "system",
            "id": "summary",
            "name": "Summary",
            "path": "/x.md",
            "status": "completed",
        }
    ]
    await repo.set_task_prompt_results(task, prompt_results)

    # Re-read from DB to confirm flush round-trips
    await session.refresh(task)
    assert task.options["prompt_results"] == prompt_results
