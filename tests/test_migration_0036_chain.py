"""Migration 0036 through alembic itself, on real rows (vts-lzad).

The data logic has unit tests in test_drop_task_content_migration.py, which run
the two halves on a `create_all` schema. They cannot see the migration file:
the DDL around the data step, the revision wiring, or whether the chain still
runs end to end. This runs the real thing against the test database:

    upgrade 0035 -> seed -> upgrade head -> downgrade 0035 -> upgrade head

The downgrade is the only way back to 1.10.1, so it gets the same scrutiny as
the upgrade: the refill and the copy/strip run on rows the test seeded at 0035.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests._db import TEST_DATABASE_URL, make_test_engine
from vts.db.base import Base

ROOT = Path(__file__).resolve().parent.parent
BEFORE = "0035_recording_listed_at"
USER = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


def _alembic_config() -> Config:
    # No ini file: the ini's fileConfig() would reset the logging of every test
    # that runs after this one. Everything alembic needs is set here.
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _sql(statements: list[tuple[str, dict]]) -> list:
    """Run statements in one transaction; return the rows of the last one."""
    async def go():
        engine = make_test_engine()
        try:
            async with engine.begin() as conn:
                result = None
                for stmt, params in statements:
                    result = await conn.execute(text(stmt), params)
                return [dict(r) for r in result.mappings().all()] if result.returns_rows else []
        finally:
            await engine.dispose()
    return asyncio.run(go())


def _reset_schema() -> None:
    async def go():
        engine = make_test_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                # A run that died between revisions leaves tables the head
                # metadata does not know (or knows differently).
                left = await conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                ))
                for (name,) in left.all():
                    await conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
        finally:
            await engine.dispose()
    asyncio.run(go())


def _columns(table: str) -> set[str]:
    rows = _sql([(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t", {"t": table},
    )])
    return {r["column_name"] for r in rows}


@pytest.fixture
def chain(monkeypatch):
    """A config that targets the isolated test database, on a clean schema."""
    monkeypatch.setenv("VTS_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VTS_ENVIRONMENT", "dev")
    monkeypatch.delenv("VTS_ALLOW_PROD_MIGRATIONS", raising=False)
    _reset_schema()
    yield _alembic_config()
    _reset_schema()


def _seed_at_0035(tmp_path: Path) -> dict:
    memo = tmp_path / "memo.md"
    memo.write_text("memo", encoding="utf-8")
    results = [{"source": "user", "id": "memo", "status": "completed", "path": str(memo)}]
    ids = {k: uuid.uuid4() for k in ("done", "done_rec", "open", "open_rec", "orphan")}
    stmts = [
        ("INSERT INTO users (id, username, created_at) VALUES (:id, 'chain', now())", {"id": USER}),
        # A finished task whose recording already holds the content.
        (
            "INSERT INTO tasks (id, user_id, source_url, status, artifact_dir, options, "
            "transcript_path, summary_path, created_at, updated_at) VALUES (:id, :u, "
            "'https://example.com/done', 'completed', '/a', CAST(:o AS json), "
            "'/a/outputs/transcript.txt', '/a/outputs/summary.md', now(), now())",
            {"id": ids["done"], "u": USER,
             "o": json.dumps({"transcript": True, "prompt_results": results})},
        ),
        (
            "INSERT INTO recordings (id, user_id, source_task_id, artifact_dir, "
            "transcript_path, summary_path, prompt_results, meta, created_at, updated_at) VALUES (:id, :u, :t, '/a', "
            "'/a/outputs/transcript.txt', '/a/outputs/summary.md', CAST(:r AS json), "
            "CAST(:m AS json), now(), now())",
            {"id": ids["done_rec"], "u": USER, "t": ids["done"], "r": json.dumps(results),
             "m": json.dumps({"origin": "upload", "prompt_results": results})},
        ),
        # An unfinished task: the content is on the task only.
        (
            "INSERT INTO tasks (id, user_id, source_url, status, artifact_dir, options, "
            "transcript_path, summary_path, created_at, updated_at) VALUES (:id, :u, 'https://example.com/open', "
            "'running', '/b', CAST(:o AS json), '/b/outputs/transcript.txt', "
            "'/b/outputs/summary.md', now(), now())",
            {"id": ids["open"], "u": USER, "o": json.dumps({"prompt_results": results})},
        ),
        (
            "INSERT INTO recordings (id, user_id, source_task_id, artifact_dir, created_at, "
            "updated_at) VALUES (:id, :u, :t, '/b', now(), now())",
            {"id": ids["open_rec"], "u": USER, "t": ids["open"]},
        ),
        # A task with no recording: nowhere to copy to.
        (
            "INSERT INTO tasks (id, user_id, source_url, status, artifact_dir, options, "
            "transcript_path, created_at, updated_at) VALUES (:id, :u, 'https://example.com/orphan', 'failed', "
            "'/c', CAST(:o AS json), '/c/outputs/transcript.txt', now(), now())",
            {"id": ids["orphan"], "u": USER, "o": json.dumps({"language": "de"})},
        ),
    ]
    _sql(stmts)
    return {"ids": ids, "results": results}


def _recording(rec_id: uuid.UUID) -> dict:
    return _sql([(
        "SELECT transcript_path, summary_path, prompt_results, meta FROM recordings "
        "WHERE id = :id", {"id": rec_id},
    )])[0]


def _task_with_columns(task_id: uuid.UUID) -> dict:
    return _sql([(
        "SELECT transcript_path, summary_path, options FROM tasks WHERE id = :id",
        {"id": task_id},
    )])[0]


def _task_options(task_id: uuid.UUID) -> dict:
    return _sql([("SELECT options FROM tasks WHERE id = :id", {"id": task_id})])[0]["options"]


def test_upgrade_downgrade_upgrade_on_real_rows(chain, tmp_path):
    cfg = chain
    command.upgrade(cfg, BEFORE)
    assert {"transcript_path", "summary_path"} <= _columns("tasks")
    seeded = _seed_at_0035(tmp_path)
    ids, results = seeded["ids"], seeded["results"]

    # Upgrade: copy what the recording lacks, strip the old copies, drop the columns.
    command.upgrade(cfg, "head")
    assert not {"transcript_path", "summary_path"} & _columns("tasks")
    assert {"transcript_path", "summary_path", "prompt_results"} <= _columns("recordings")
    opened = _recording(ids["open_rec"])
    assert opened["transcript_path"] == "/b/outputs/transcript.txt"
    assert opened["summary_path"] == "/b/outputs/summary.md"
    assert opened["prompt_results"] == results
    assert _recording(ids["done_rec"])["prompt_results"] == results
    assert _recording(ids["done_rec"])["meta"] == {"origin": "upload"}
    assert _task_options(ids["done"]) == {"transcript": True}
    assert _task_options(ids["open"]) == {}
    assert _task_options(ids["orphan"]) == {"language": "de"}

    # Downgrade: the columns come back, filled from each task's recording.
    command.downgrade(cfg, BEFORE)
    assert {"transcript_path", "summary_path"} <= _columns("tasks")
    done = _task_with_columns(ids["done"])
    assert done["transcript_path"] == "/a/outputs/transcript.txt"
    assert done["summary_path"] == "/a/outputs/summary.md"
    assert done["options"] == {"transcript": True, "prompt_results": results}
    # The task that had content only on itself now has it, through its recording.
    moved = _task_with_columns(ids["open"])
    assert moved["transcript_path"] == "/b/outputs/transcript.txt"
    assert moved["options"] == {"prompt_results": results}
    # No recording: nothing to restore from, and the path is gone for good.
    orphan = _task_with_columns(ids["orphan"])
    assert orphan["transcript_path"] is None
    assert orphan["options"] == {"language": "de"}
    # Recordings carry the 1.10.1 snapshot again.
    assert _recording(ids["done_rec"])["meta"] == {"origin": "upload", "prompt_results": results}
    assert _recording(ids["open_rec"])["meta"] == {"prompt_results": results}

    # And up again: the chain survives a second pass over the refilled rows.
    command.upgrade(cfg, "head")
    assert not {"transcript_path", "summary_path"} & _columns("tasks")
    assert _task_options(ids["done"]) == {"transcript": True}
    assert _task_options(ids["open"]) == {}
    assert _recording(ids["done_rec"])["meta"] == {"origin": "upload"}
    assert _recording(ids["done_rec"])["prompt_results"] == results
    assert _recording(ids["open_rec"])["transcript_path"] == "/b/outputs/transcript.txt"
