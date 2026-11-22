"""Content backfill: every task gets its recording, recordings get their files (vts-lzad)."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import AsrSegment, Recording, RecordingState, Task, TaskStatus, User
from vts.services.recording_backfill import backfill_recording_content

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # The backfill runs at revision 0034, when tasks still had these
        # columns; the model no longer declares them (vts-lzad).
        await conn.execute(text(
            "ALTER TABLE tasks ADD COLUMN transcript_path TEXT, ADD COLUMN summary_path TEXT"
        ))
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _set_task_paths(s, task_id, *, transcript=None, summary=None) -> None:
    await s.execute(
        text("UPDATE tasks SET transcript_path = :t, summary_path = :s WHERE id = :id"),
        {"t": transcript, "s": summary, "id": task_id},
    )


async def _run(factory, *, available: bool = True) -> dict[str, int]:
    async with factory() as s:
        async with s.bind.begin() as conn:
            return await conn.run_sync(
                lambda c: backfill_recording_content(c, artifacts_available=available)
            )


async def _recordings(factory) -> dict[str, Recording]:
    async with factory() as s:
        rows = (await s.execute(select(Recording))).scalars().all()
    return {r.title: r for r in rows}


def _snapshot(recordings: dict[str, Recording]) -> dict[str, tuple]:
    return {
        title: (
            r.id, r.source_task_id, r.state, r.redacted_path, r.prompt_results, r.meta,
            r.transcript_path, r.summary_path, r.duration_sec, r.language, r.updated_at,
        )
        for title, r in recordings.items()
    }


def _write(path: Path, text: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_task_without_recording_gets_one(factory, tmp_path):
    adir = tmp_path / "t1"
    transcript = _write(adir / "outputs" / "transcript.txt")
    async with factory() as s:
        task = Task(
            user_id=_USER, source_url="file://Team%20sync.mp3", source_title=None,
            status=TaskStatus.completed, artifact_dir=str(adir),
            options={"detected_language": "russian"},
        )
        s.add(task)
        await s.flush()
        await _set_task_paths(s, task.id, transcript=transcript)
        s.add_all([
            AsrSegment(task_id=task.id, segment_index=0, start_sec=0.0, end_sec=12.5),
            AsrSegment(task_id=task.id, segment_index=1, start_sec=12.5, end_sec=42.0),
        ])
        await s.commit()
        task_id, created_at = task.id, task.created_at

    counts = await _run(factory)

    recs = await _recordings(factory)
    assert list(recs) == ["Team sync.mp3"]
    rec = recs["Team sync.mp3"]
    assert counts == {"created": 1, "redacted": 0, "prompt_results": 0}
    assert rec.source_task_id == task_id
    assert rec.user_id == _USER
    assert rec.title_is_custom is False
    assert rec.source_url == "file://Team%20sync.mp3"
    assert rec.artifact_dir == str(adir)
    assert rec.transcript_path == transcript
    assert rec.summary_path is None
    assert rec.state is RecordingState.complete
    assert rec.language == "ru"
    assert rec.duration_sec == 42.0
    assert rec.recorded_at == created_at
    assert rec.meta == {"prompt_results": []}
    assert rec.prompt_results == []
    assert rec.redacted_path is None


@pytest.mark.asyncio
async def test_existing_recording_gets_files_that_still_exist(factory, tmp_path):
    # An archived task: its redacted transcript is gone, one prompt result
    # file is gone, the other is still there.
    gone = tmp_path / "gone"
    kept_dir = tmp_path / "kept"
    memo = _write(gone / "outputs" / "prompt_memo.md")
    missing = str(gone / "outputs" / "prompt_todo.md")
    redacted = _write(kept_dir / "outputs" / "redacted_transcript.txt", "")
    results = [
        {"source": "user", "id": "memo", "status": "completed", "path": memo},
        {"source": "user", "id": "todo", "status": "completed", "path": missing},
        {"source": "user", "id": "broken", "status": "failed"},
    ]
    async with factory() as s:
        archived = Task(
            user_id=_USER, source_url="https://example.com/a", status=TaskStatus.archived,
            artifact_dir=str(gone),
            options={"prompts": ["user:memo", "user:todo"], "prompt_results": results},
        )
        live = Task(
            user_id=_USER, source_url="https://example.com/b", status=TaskStatus.completed,
            artifact_dir=str(kept_dir), options={},
        )
        s.add_all([archived, live])
        await s.flush()
        s.add_all([
            Recording(
                user_id=_USER, source_task_id=archived.id, title="archived",
                artifact_dir=str(gone), transcript_path="/t",
                state=RecordingState.complete,
            ),
            Recording(
                user_id=_USER, source_task_id=live.id, title="live",
                artifact_dir=str(kept_dir), transcript_path="/t",
                state=RecordingState.complete,
            ),
        ])
        await s.commit()

    counts = await _run(factory)

    recs = await _recordings(factory)
    assert counts == {"created": 0, "redacted": 1, "prompt_results": 1}
    assert recs["archived"].redacted_path is None
    assert recs["archived"].prompt_results == [results[0]]
    # An empty redacted file is a real result (nothing to redact), not absence.
    assert recs["live"].redacted_path == redacted
    assert recs["live"].prompt_results == []
    # State is not this backfill's business.
    assert recs["archived"].state is RecordingState.complete


@pytest.mark.asyncio
async def test_values_the_live_code_wrote_are_kept(factory, tmp_path):
    adir = tmp_path / "t"
    _write(adir / "outputs" / "redacted_transcript.txt")
    other = _write(adir / "outputs" / "prompt_other.md")
    mine = [{"source": "user", "id": "mine", "status": "completed", "path": "/written/live"}]
    async with factory() as s:
        task = Task(
            user_id=_USER, source_url="https://example.com/c", status=TaskStatus.completed,
            artifact_dir=str(adir),
            options={"prompt_results": [
                {"source": "user", "id": "other", "status": "completed", "path": other},
            ]},
        )
        s.add(task)
        await s.flush()
        s.add(Recording(
            user_id=_USER, source_task_id=task.id, title="live", artifact_dir=str(adir),
            transcript_path="/t", redacted_path="/written/redacted", prompt_results=mine,
        ))
        await s.commit()

    counts = await _run(factory)

    rec = (await _recordings(factory))["live"]
    assert counts == {"created": 0, "redacted": 0, "prompt_results": 0}
    assert rec.redacted_path == "/written/redacted"
    assert rec.prompt_results == mine


@pytest.mark.asyncio
async def test_detached_recording_is_left_alone(factory, tmp_path):
    adir = tmp_path / "d"
    _write(adir / "outputs" / "redacted_transcript.txt")
    async with factory() as s:
        s.add(Recording(
            user_id=_USER, source_task_id=None, title="detached", artifact_dir=str(adir),
            transcript_path="/t", state=RecordingState.complete,
        ))
        await s.commit()

    counts = await _run(factory)

    assert counts == {"created": 0, "redacted": 0, "prompt_results": 0}
    assert (await _recordings(factory))["detached"].redacted_path is None


@pytest.mark.asyncio
async def test_root_check_missing_still_fills_reachable_files(factory, tmp_path, capsys):
    # The root check can be wrong (configured artifacts_root differs from the
    # mount), and the migration runs once: a skipped fill would be lost for
    # good. Each file is checked on its own, so the check only warns.
    adir = tmp_path / "n"
    redacted = _write(adir / "outputs" / "redacted_transcript.txt")
    memo = _write(adir / "outputs" / "prompt_memo.md")
    results = [{"source": "user", "id": "memo", "status": "completed", "path": memo}]
    async with factory() as s:
        s.add(Task(
            user_id=_USER, source_url="https://example.com/n", source_title="no volume",
            status=TaskStatus.running, artifact_dir=str(adir),
            options={"prompt_results": results},
        ))
        await s.commit()

    counts = await _run(factory, available=False)

    rec = (await _recordings(factory))["no volume"]
    assert counts == {"created": 1, "redacted": 1, "prompt_results": 1}
    assert rec.state is RecordingState.processing
    assert rec.redacted_path == redacted
    assert rec.prompt_results == results
    assert "artifacts root not found" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_second_run_changes_nothing(factory, tmp_path):
    adir = tmp_path / "r"
    _write(adir / "outputs" / "redacted_transcript.txt")
    memo = _write(adir / "outputs" / "prompt_memo.md")
    results = [{"source": "user", "id": "memo", "status": "completed", "path": memo}]
    async with factory() as s:
        with_rec = Task(
            user_id=_USER, source_url="https://example.com/w", status=TaskStatus.completed,
            artifact_dir=str(adir), options={"prompt_results": results},
        )
        s.add_all([
            with_rec,
            Task(
                user_id=_USER, source_url="https://example.com/new", source_title="new",
                status=TaskStatus.failed, artifact_dir=str(adir), options={},
            ),
        ])
        await s.flush()
        s.add(Recording(
            user_id=_USER, source_task_id=with_rec.id, title="existing",
            artifact_dir=str(adir), transcript_path="/t",
        ))
        await s.commit()

    first = await _run(factory)
    before = _snapshot(await _recordings(factory))
    second = await _run(factory)
    after = _snapshot(await _recordings(factory))

    assert first == {"created": 1, "redacted": 2, "prompt_results": 1}
    assert second == {"created": 0, "redacted": 0, "prompt_results": 0}
    assert after == before
    assert set(after) == {"existing", "new"}


@pytest.mark.asyncio
async def test_artifacts_available_detects_a_missing_or_empty_volume(factory, tmp_path):
    from vts.services.recording_backfill import artifacts_available

    async def check(root: Path) -> bool:
        async with factory() as s:
            async with s.bind.begin() as conn:
                return await conn.run_sync(lambda c: artifacts_available(c, root))

    root = tmp_path / "data"
    assert await check(root) is False  # not mounted
    root.mkdir()
    assert await check(root) is True  # mounted, no tasks yet
    async with factory() as s:
        s.add(Task(
            user_id=_USER, source_url="https://example.com/e", status=TaskStatus.completed,
            artifact_dir=str(root / "task"), options={},
        ))
        await s.commit()
    assert await check(root) is False  # empty mount point: no task directory exists
    (root / "task").mkdir()
    assert await check(root) is True


@pytest.mark.asyncio
async def test_unreadable_path_counts_as_missing(factory, tmp_path, monkeypatch):
    # PermissionError from a stat must not abort the migration (it runs in the
    # init container, where an exception blocks startup).
    adir = tmp_path / "p"
    memo = _write(adir / "outputs" / "prompt_memo.md")
    results = [{"source": "user", "id": "memo", "status": "completed", "path": memo}]
    async with factory() as s:
        s.add(Task(
            user_id=_USER, source_url="https://example.com/p", source_title="perm",
            status=TaskStatus.completed, artifact_dir=str(adir),
            options={"prompt_results": results},
        ))
        await s.commit()

    def _boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", _boom)
    monkeypatch.setattr(Path, "is_dir", _boom)

    counts = await _run(factory)

    rec = (await _recordings(factory))["perm"]
    assert counts["redacted"] == 0 and counts["prompt_results"] == 0
    assert rec.prompt_results == []


@pytest.mark.asyncio
async def test_created_recording_keeps_the_task_creation_time_and_lists_there(factory, tmp_path):
    """A recording made by the backfill is dated by its task, not by the migration.

    Otherwise backfill_listed_at stamps all of them with one migration-time
    instant and they sort above every real recording (vts-lzad).
    """
    from datetime import timedelta

    from sqlalchemy import select

    from vts.db.models import utcnow
    from vts.services.recording_backfill import backfill_listed_at

    old = utcnow() - timedelta(days=90)
    adir = tmp_path / "old"
    transcript = _write(adir / "outputs" / "transcript.txt")
    async with factory() as s:
        task = Task(
            user_id=_USER, source_url="file://old.mp3", status=TaskStatus.completed,
            artifact_dir=str(adir), options={},
            created_at=old,
        )
        s.add(task)
        await s.flush()
        await _set_task_paths(s, task.id, transcript=transcript)
        await s.commit()

    await _run(factory)
    async with factory() as s:
        async with s.bind.begin() as conn:
            await conn.run_sync(backfill_listed_at)
    async with factory() as s:
        rec = (await s.execute(select(Recording))).scalars().one()
    assert rec.created_at == old
    assert rec.listed_at == old
