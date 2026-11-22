"""Migration 0036: the task's content columns go, and come back on downgrade (vts-lzad).

The downgrade is the only way back to 1.10.1, whose code still reads
`tasks.transcript_path` / `summary_path` and `options["prompt_results"]` and
copies them onto the recording on every upsert. So after a downgrade each
task must carry exactly what its recording holds, or the first upsert under
1.10.1 would blank the recording.

The functions run against a `create_all` schema with the old task columns
added by hand, standing in for the migration's DDL.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, Task, TaskStatus, User
from vts.services.recording_backfill import refill_task_content, strip_task_content

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_RESULTS = [
    {"source": "user", "id": "memo", "status": "completed", "path": "/a/summary/results/memo.md"},
    {"source": "system", "id": "summary", "status": "completed", "path": "/a/summary/final.md"},
]


@pytest.fixture
async def engine():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await _add_task_columns(conn)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _add_task_columns(conn) -> None:
    await conn.execute(text(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS transcript_path TEXT, "
        "ADD COLUMN IF NOT EXISTS summary_path TEXT"
    ))


async def _drop_task_columns(conn) -> None:
    await conn.execute(text(
        "ALTER TABLE tasks DROP COLUMN transcript_path, DROP COLUMN summary_path"
    ))


async def _seed(engine) -> dict[str, uuid.UUID]:
    """Three shapes: a task with its recording, a task without one, and a
    recording whose task is gone."""
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        linked = Task(
            user_id=_USER, source_url="https://example.com/linked",
            status=TaskStatus.completed, artifact_dir="/a",
            options={"transcript": True, "prompt_results": [dict(r) for r in _RESULTS]},
        )
        lonely = Task(
            user_id=_USER, source_url="https://example.com/lonely",
            status=TaskStatus.failed, artifact_dir="/b",
            options={"language": "de"},
        )
        s.add_all([linked, lonely])
        await s.flush()
        rec = Recording(
            user_id=_USER, source_task_id=linked.id, artifact_dir="/a",
            transcript_path="/a/outputs/transcript.txt",
            summary_path="/a/outputs/summary.md",
            prompt_results=[dict(r) for r in _RESULTS],
            meta={"prompt_results": [dict(r) for r in _RESULTS], "origin": "upload"},
        )
        detached = Recording(
            user_id=_USER, source_task_id=None, artifact_dir="/c",
            transcript_path="/c/outputs/transcript.txt",
            prompt_results=[dict(_RESULTS[0])],
            meta={"prompt_results": [dict(_RESULTS[0])]},
        )
        s.add_all([rec, detached])
        await s.commit()
        ids = {"linked": linked.id, "lonely": lonely.id, "rec": rec.id, "detached": detached.id}
    async with engine.begin() as conn:
        # The 1.10.1 shape: the task carries the same paths as its recording.
        await conn.execute(
            text(
                "UPDATE tasks SET transcript_path = :t, summary_path = :s WHERE id = :id"
            ),
            {"t": "/a/outputs/transcript.txt", "s": "/a/outputs/summary.md", "id": ids["linked"]},
        )
    return ids


async def _rows(engine, sql: str) -> dict:
    async with engine.connect() as conn:
        return {r["id"]: dict(r) for r in (await conn.execute(text(sql))).mappings().all()}


async def _tasks(engine) -> dict:
    return await _rows(engine, "SELECT id, options, transcript_path, summary_path FROM tasks")


async def _tasks_without_columns(engine) -> dict:
    return await _rows(engine, "SELECT id, options FROM tasks")


async def _recordings(engine) -> dict:
    return await _rows(engine, "SELECT id, meta, prompt_results, updated_at FROM recordings")


async def _run(engine, fn):
    async with engine.begin() as conn:
        return await conn.run_sync(fn)


@pytest.mark.asyncio
async def test_upgrade_strips_the_old_copies_and_keeps_everything_else(engine):
    ids = await _seed(engine)
    before = await _recordings(engine)

    counts = await _run(engine, strip_task_content)

    tasks = await _tasks(engine)
    recs = await _recordings(engine)
    assert tasks[ids["linked"]]["options"] == {"transcript": True}
    assert tasks[ids["lonely"]]["options"] == {"language": "de"}
    assert recs[ids["rec"]]["meta"] == {"origin": "upload"}
    assert recs[ids["detached"]]["meta"] == {}
    # The recording's own column is the content now; it is not touched.
    assert recs[ids["rec"]]["prompt_results"] == _RESULTS
    assert recs[ids["rec"]]["updated_at"] == before[ids["rec"]]["updated_at"]
    assert counts == {"task_options": 1, "recording_meta": 2, "orphaned_paths": 0}


@pytest.mark.asyncio
async def test_upgrade_counts_task_paths_the_recording_lacks(engine):
    ids = await _seed(engine)
    async with engine.begin() as conn:
        # Unfinished under 1.10.1: the task has a transcript, the completion
        # upsert that copies it never ran.
        await conn.execute(
            text("UPDATE tasks SET transcript_path = '/b/outputs/transcript.txt' WHERE id = :id"),
            {"id": ids["lonely"]},
        )

    counts = await _run(engine, strip_task_content)

    assert counts["orphaned_paths"] == 1


@pytest.mark.asyncio
async def test_downgrade_gives_each_task_what_its_recording_holds(engine):
    ids = await _seed(engine)
    await _run(engine, strip_task_content)
    async with engine.begin() as conn:
        await _drop_task_columns(conn)
    # Content written after the upgrade lives on the recording only.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE recordings SET summary_path = '/a/summary/final.md', "
                "prompt_results = CAST(:r AS json) WHERE id = :id"
            ),
            {"r": json.dumps(_RESULTS[:1]), "id": ids["rec"]},
        )
        await _add_task_columns(conn)
    before = await _recordings(engine)

    counts = await _run(engine, refill_task_content)

    tasks = await _tasks(engine)
    recs = await _recordings(engine)
    linked = tasks[ids["linked"]]
    assert linked["transcript_path"] == "/a/outputs/transcript.txt"
    assert linked["summary_path"] == "/a/summary/final.md"
    assert linked["options"] == {"transcript": True, "prompt_results": _RESULTS[:1]}
    lonely = tasks[ids["lonely"]]
    assert (lonely["transcript_path"], lonely["summary_path"]) == (None, None)
    assert lonely["options"] == {"language": "de"}
    assert recs[ids["rec"]]["meta"] == {"origin": "upload", "prompt_results": _RESULTS[:1]}
    assert recs[ids["detached"]]["meta"] == {"prompt_results": [_RESULTS[0]]}
    assert recs[ids["rec"]]["updated_at"] == before[ids["rec"]]["updated_at"]
    assert counts == {"tasks": 1, "recordings": 2}


@pytest.mark.asyncio
async def test_round_trip_restores_the_1_10_1_shape(engine):
    ids = await _seed(engine)
    seeded_tasks = await _tasks(engine)
    seeded_recs = await _recordings(engine)

    await _run(engine, strip_task_content)
    async with engine.begin() as conn:
        await _drop_task_columns(conn)
    assert set(await _tasks_without_columns(engine)) == set(seeded_tasks)
    async with engine.begin() as conn:
        await _add_task_columns(conn)
    await _run(engine, refill_task_content)

    assert await _tasks(engine) == {
        ids["linked"]: seeded_tasks[ids["linked"]],
        ids["lonely"]: seeded_tasks[ids["lonely"]],
    }
    recs = await _recordings(engine)
    for rid in (ids["rec"], ids["detached"]):
        assert recs[rid]["meta"] == seeded_recs[rid]["meta"]


@pytest.mark.asyncio
async def test_both_directions_are_safe_to_repeat(engine):
    ids = await _seed(engine)
    await _run(engine, strip_task_content)
    assert await _run(engine, strip_task_content) == {
        "task_options": 0, "recording_meta": 0, "orphaned_paths": 0,
    }
    await _run(engine, refill_task_content)
    once = await _tasks(engine), await _recordings(engine)
    await _run(engine, refill_task_content)
    assert (await _tasks(engine), await _recordings(engine)) == once
    assert once[0][ids["linked"]]["transcript_path"] == "/a/outputs/transcript.txt"
