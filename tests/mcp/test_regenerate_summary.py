"""regenerate_summary: re-run a recording's prompts from the MCP summary view
(vts-vtma.8), the same restart the web UI's "restart the final summary" does.

Against the real database: the tool's work IS the state change — the task's
prompt set swapped, every previous result cleared, the task queued — so a
fake repo would test the fake.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastmcp import Client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.core.config import Settings
from vts.db.base import Base
from vts.db.models import Prompt, Step, StepStatus, Task, TaskStatus, User
from vts.db.repo import Repo

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e8")

_STEPS = ["download", "extract_audio", "trim_initial_silence", "segment_audio",
          "detect_language", "transcribe_segments", "merge_transcript",
          "prepare_llama_model", "prepare_summary_chunks", "summarize_windows",
          "pack_window_notes", "summarize_final", "finalize:user:a"]


@dataclass
class _User:
    id: str = str(_USER)
    username: str = "owner"


class _FakeRedis:
    def __init__(self):
        self.published = []

    async def publish(self, channel, message):
        self.published.append(channel)
        return 0

    async def aclose(self):
        pass


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="owner"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def client(factory, monkeypatch):
    import vts.mcp.tools_registry.apps as apps_mod
    import vts.services.redis_client as redis_client
    from vts.mcp.server import build_mcp_server

    redis = _FakeRedis()

    async def _auth(_session):
        return _User(), Settings()

    monkeypatch.setattr(apps_mod, "mcp_authenticate", _auth)
    monkeypatch.setattr(apps_mod, "get_db_session_factory", lambda: factory)
    monkeypatch.setattr(redis_client, "make_redis", lambda _url: redis)
    c = Client(build_mcp_server())
    c.fake_redis = redis
    return c


async def _seed(factory, tmp_path, *, status=TaskStatus.completed, detach=False):
    """A finished task that ran [summary, user:a], its recording, and a user
    prompt 'b' to switch to."""
    async with factory() as s:
        pb = Prompt(id=uuid.uuid4(), user_id=_USER, name="B", system_prompt="b")
        s.add(pb)
        art = tmp_path / "task"
        (art / "summary").mkdir(parents=True)
        (art / "summary" / "final.md").write_text("old summary")
        task = Task(
            id=uuid.uuid4(), user_id=_USER, source_url="x", artifact_dir=str(art),
            status=status, summary_path=str(art / "summary" / "final.md"),
            options={
                "prompts": [{"source": "system", "id": "summary"}, {"source": "user", "id": "a"}],
                "prompt_results": [
                    {"source": "system", "id": "summary", "name": "S",
                     "path": str(art / "summary" / "final.md"), "status": "completed"},
                    {"source": "user", "id": "a", "name": "A",
                     "path": str(art / "summary" / "results" / "user__a.md"), "status": "completed"},
                ],
            },
        )
        s.add(task)
        for name in _STEPS:
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.flush()
        recording = await Repo(s).upsert_recording_for_task(task)
        if detach:
            recording.source_task_id = None
        await s.commit()
        return task.id, recording.id, pb.id


async def test_regenerate_reruns_every_prompt_with_the_new_set(factory, client, tmp_path):
    task_id, rec_id, pb = await _seed(factory, tmp_path)
    async with client:
        result = await client.call_tool("regenerate_summary", {
            "recording_id": str(rec_id), "prompts": ["system:summary", f"user:{pb}"],
        })
    data = result.structured_content
    assert data == {"task_id": str(task_id), "status": "queued",
                    "prompts": ["system:summary", f"user:{pb}"]}
    async with factory() as s:
        task = await Repo(s).get_task_by_id(task_id)
        assert task.status == TaskStatus.queued
        assert task.options["prompts"] == [{"source": "system", "id": "summary"},
                                           {"source": "user", "id": str(pb)}]
        # All previous results are gone, as in the web UI's restart.
        assert not task.options.get("prompt_results")
        assert task.summary_path is None
        steps = {st.name: st.status for st in task.steps}
        assert "finalize:user:a" not in steps
        assert steps["summarize_final"] == StepStatus.pending
        assert steps[f"finalize:user:{pb}"] == StepStatus.pending
        assert steps["merge_transcript"] == StepStatus.completed
    assert client.fake_redis.published, "the worker was not told a task is queued"


async def test_a_recording_without_its_task_cannot_rerun(factory, client, tmp_path):
    _task_id, rec_id, _pb = await _seed(factory, tmp_path, detach=True)
    async with client:
        result = await client.call_tool("regenerate_summary", {
            "recording_id": str(rec_id), "prompts": ["system:summary"],
        }, raise_on_error=False)
    assert result.is_error
    assert "deleted" in result.content[0].text


async def test_a_running_task_is_refused_and_left_alone(factory, client, tmp_path):
    task_id, rec_id, _pb = await _seed(factory, tmp_path, status=TaskStatus.running)
    async with client:
        result = await client.call_tool("regenerate_summary", {
            "recording_id": str(rec_id), "prompts": ["system:summary"],
        }, raise_on_error=False)
    assert result.is_error
    assert "running" in result.content[0].text
    async with factory() as s:
        task = await Repo(s).get_task_by_id(task_id)
        assert task.status == TaskStatus.running
        assert len(task.options["prompt_results"]) == 2


@pytest.mark.parametrize("prompts, fragment", [
    ([], "at least one"),
    (["system:nope"], "unknown system prompt"),
    ([f"user:{uuid.uuid4()}"], "unknown prompt"),
    (["garbage"], "invalid prompt"),
])
async def test_bad_prompt_sets_change_nothing(factory, client, tmp_path, prompts, fragment):
    task_id, rec_id, _pb = await _seed(factory, tmp_path)
    async with client:
        result = await client.call_tool("regenerate_summary", {
            "recording_id": str(rec_id), "prompts": prompts,
        }, raise_on_error=False)
    assert result.is_error
    assert fragment in result.content[0].text
    async with factory() as s:
        task = await Repo(s).get_task_by_id(task_id)
        assert task.status == TaskStatus.completed
        assert len(task.options["prompt_results"]) == 2


async def test_the_tool_is_marked_destructive():
    # The previous results are cleared before the new ones exist: a client
    # should confirm, as for a delete.
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as c:
        tool = {t.name: t for t in await c.list_tools()}["regenerate_summary"]
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is False
