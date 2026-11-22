"""Artifacts addressed by RECORDING, not by task (vts-lib3).

Search returns `recording_id` because that is the identifier which lasts — and
then there was nothing to fetch with it: every artifact endpoint and every MCP
tool took a `task_id`. So the one identifier a client is told to keep could not
be used to read anything, and a recording whose task had been deleted was
unreachable even though its files were still on disk.

A recording carries its own `artifact_dir`. Reading through it needs no task at
all, which is the point: a recording is the lasting object and a task is a job
that produced it.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    read_recording_transcript,
    recording_transcript_entries,
)

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _seed_artifacts(root):
    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "transcript.txt").write_text("Привет. Это транскрипт.", encoding="utf-8")
    (outputs / "transcript.json").write_text(
        json.dumps({"entries": [
            {"start": 0.0, "end": 2.0, "text": "Привет.", "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 6.0, "text": "Это транскрипт.", "speaker": "SPEAKER_01"},
        ]}),
        encoding="utf-8",
    )
    (outputs / "summary.md").write_text("# Итог\n\nКоротко.", encoding="utf-8")
    return outputs


async def _recording(session, tmp_path, *, detach=False):
    repo = Repo(session)
    outputs = _seed_artifacts(tmp_path)
    task = await repo.create_task(
        user_id=_USER, source_url="file://clip.m4a", options={},
        artifact_dir=str(tmp_path),
    )
    await repo.set_task_content(
        task,
        transcript_path=str(outputs / "transcript.txt"),
        summary_path=str(outputs / "summary.md"),
    )
    recording = await repo.upsert_recording_for_task(task)
    if detach:
        # The task is gone; the recording and its files remain.
        await session.delete(task)
        recording.source_task_id = None
    await session.commit()
    return recording


@pytest.mark.asyncio
async def test_a_transcript_reads_from_the_recording(factory, tmp_path):
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        text = read_recording_transcript(recording, "raw")
        assert "Это транскрипт" in text


@pytest.mark.asyncio
async def test_a_transcript_still_reads_after_the_task_is_gone(factory, tmp_path):
    """The whole reason for addressing artifacts by recording.

    A recording outlives its task, keeps its own artifact_dir, and its files are
    untouched — so a deleted task must not make its transcript unreadable.
    """
    async with factory() as session:
        recording = await _recording(session, tmp_path, detach=True)
        assert recording.source_task_id is None
        text = read_recording_transcript(recording, "raw")
        assert "Это транскрипт" in text


@pytest.mark.asyncio
async def test_a_summary_reads_from_the_recording(factory, tmp_path):
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        assert "Итог" in read_recording_transcript(recording, "summary")


@pytest.mark.asyncio
async def test_a_missing_artifact_says_which_one(factory, tmp_path):
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        with pytest.raises(RecordingArtifactMissing) as exc:
            read_recording_transcript(recording, "redacted")
        assert "redacted" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_entries_carry_timecodes_and_speakers(factory, tmp_path):
    """Flat text cannot be cited; the structured form is what a client needs.

    The existing MCP get_transcript returns `content: str`, so an assistant that
    found a passage had no way to say WHEN it was said.
    """
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        entries = recording_transcript_entries(recording)
        assert len(entries) == 2
        assert entries[0]["start"] == 0.0
        assert entries[1]["speaker"] == "SPEAKER_01"


@pytest.mark.asyncio
async def test_entries_can_be_windowed_around_a_moment(factory, tmp_path):
    """A search hit points at a second; the client wants the passage AROUND it.

    Returning the whole transcript to show one quote in context is wasteful for
    a two-hour recording and pushes the interesting part out of an LLM's
    attention.
    """
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        # 3.0 ± 1.5 is [1.5, 4.5]. "Привет." runs 0–2, so it OVERLAPS the window
        # and comes back: an entry straddling the edge is part of the passage a
        # reader needs, and cutting it would quote someone mid-sentence.
        around = recording_transcript_entries(recording, around_sec=3.0, window_sec=1.5)
        assert [e["text"] for e in around] == ["Привет.", "Это транскрипт."]

        # A window that clears the first entry entirely leaves it out.
        later = recording_transcript_entries(recording, around_sec=5.0, window_sec=0.5)
        assert [e["text"] for e in later] == ["Это транскрипт."]


@pytest.mark.asyncio
async def test_a_window_outside_the_recording_is_empty_not_an_error(factory, tmp_path):
    async with factory() as session:
        recording = await _recording(session, tmp_path)
        assert recording_transcript_entries(recording, around_sec=9999.0, window_sec=5.0) == []


# ------------------------------------------------------------------ HTTP API

@pytest.mark.asyncio
async def test_the_endpoint_serves_a_transcript_by_recording(authed_app, client, tmp_path):
    from tests.conftest import _TEST_USER_ID

    _app, session_factory = authed_app
    async with session_factory() as session:
        repo = Repo(session)
        outputs = _seed_artifacts(tmp_path)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID), source_url="file://clip.m4a",
            options={}, artifact_dir=str(tmp_path),
        )
        await repo.set_task_content(task, transcript_path=str(outputs / "transcript.txt"))
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        rec_id = recording.id

    r = await client.get(f"/api/recordings/{rec_id}/transcript")
    assert r.status_code == 200, r.text
    assert "Это транскрипт" in r.json()["content"]


@pytest.mark.asyncio
async def test_the_endpoint_still_works_after_the_task_is_deleted(
    authed_app, client, tmp_path
):
    """The reason this endpoint exists.

    Expanding a search hit used to mean linking to /player/{task_id}, which
    404s once the task is gone — a library result depending on a job that may
    have been cleaned up long ago. Reading through the recording does not.
    """
    from tests.conftest import _TEST_USER_ID
    from vts.api._helpers.recordings import delete_task_with_recording
    from vts.db.models import Recording

    _app, session_factory = authed_app
    async with session_factory() as session:
        repo = Repo(session)
        outputs = _seed_artifacts(tmp_path)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID), source_url="file://clip.m4a",
            options={}, artifact_dir=str(tmp_path),
        )
        await repo.set_task_content(task, transcript_path=str(outputs / "transcript.txt"))
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        rec_id = recording.id
        # Detach rather than delete_task_with_recording: this is the case where
        # the recording was kept and only its task went away.
        recording.source_task_id = None
        await session.delete(task)
        await session.commit()

    r = await client.get(f"/api/recordings/{rec_id}/transcript?around_sec=3&window_sec=2")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["entries"], "the passage was unreachable once the task was gone"
    assert any("транскрипт" in e["text"] for e in payload["entries"])
    _ = Recording


@pytest.mark.asyncio
async def test_another_users_recording_is_not_readable(authed_app, client, tmp_path):
    from vts.db.models import User

    _app, session_factory = authed_app
    other = uuid.uuid4()
    async with session_factory() as session:
        session.add(User(id=other, username="someone-else"))
        await session.flush()
        repo = Repo(session)
        _seed_artifacts(tmp_path)
        task = await repo.create_task(
            user_id=other, source_url="file://private.m4a", options={},
            artifact_dir=str(tmp_path),
        )
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        rec_id = recording.id

    r = await client.get(f"/api/recordings/{rec_id}/transcript")
    assert r.status_code == 404


# ------------------------------------------- one read layer (vts-lzad, stage 2)

class _Rec:
    """The subset of Recording the read layer touches."""

    def __init__(self, artifact_dir, **kw):
        self.artifact_dir = str(artifact_dir)
        self.transcript_path = None
        self.summary_path = None
        self.redacted_path = None
        self.prompt_results = []
        self.meta = {}
        self.__dict__.update(kw)


def test_redacted_path_column_wins_over_the_default_name(tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "redacted_transcript.txt").write_text("default")
    custom = tmp_path / "outputs" / "custom_redacted.txt"
    custom.write_text("custom")
    assert read_recording_transcript(
        _Rec(tmp_path, redacted_path=str(custom)), "redacted"
    ) == "custom"


def test_redacted_falls_back_to_the_default_name_only_when_the_column_is_null(tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "redacted_transcript.txt").write_text("legacy")
    assert read_recording_transcript(_Rec(tmp_path), "redacted") == "legacy"
    # A set-but-missing column does not silently read the default file.
    with pytest.raises(RecordingArtifactMissing):
        read_recording_transcript(
            _Rec(tmp_path, redacted_path=str(tmp_path / "gone.txt")), "redacted"
        )


def test_result_entries_come_from_the_column_not_meta(tmp_path):
    from vts.services.recording_artifacts import recording_result_entries

    rec = _Rec(
        tmp_path,
        prompt_results=[{"source": "user", "id": "real"}, "junk"],
        meta={"prompt_results": [{"source": "user", "id": "stale"}]},
    )
    assert recording_result_entries(rec) == [{"source": "user", "id": "real"}]


def test_read_result_reads_the_entry_path_and_ignores_stale_meta(tmp_path):
    from vts.services.recording_artifacts import read_recording_result

    real = tmp_path / "memo.md"
    real.write_text("real memo")
    rec = _Rec(
        tmp_path,
        prompt_results=[{"source": "user", "id": "m", "path": str(real)}],
        meta={"prompt_results": [{"source": "user", "id": "m", "path": str(tmp_path / "stale.md")}]},
    )
    assert read_recording_result(rec, "user", "m") == "real memo"


def test_system_summary_falls_back_to_summary_path(tmp_path):
    from vts.services.recording_artifacts import (
        read_recording_result,
        recording_result_path,
    )

    summary = tmp_path / "outputs" / "summary.md"
    summary.parent.mkdir()
    summary.write_text("the summary")
    rec = _Rec(tmp_path, summary_path=str(summary))
    assert recording_result_path(rec, "system", "summary") == str(summary)
    assert read_recording_result(rec, "system", "summary") == "the summary"
    assert recording_result_path(rec, "user", "summary") is None


def test_read_result_raises_when_missing_or_escaping(tmp_path):
    from vts.services.recording_artifacts import read_recording_result

    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    adir = tmp_path / "rec"
    adir.mkdir()
    rec = _Rec(adir, prompt_results=[
        {"source": "user", "id": "gone", "path": str(adir / "nope.md")},
        {"source": "user", "id": "esc", "path": str(outside)},
    ])
    for ref in ("gone", "esc", "unknown"):
        with pytest.raises(RecordingArtifactMissing):
            read_recording_result(rec, "user", ref)


@pytest.mark.parametrize(
    "kind,default_name",
    [("raw", "transcript.txt"), ("summary", "summary.md")],
)
def test_raw_and_summary_never_fall_back_to_the_default_name(tmp_path, kind, default_name):
    """vts-lzad: a NULL stored path means "not produced (yet)" — a regeneration
    clears summary_path while the previous outputs/summary.md may still be on
    disk. Serving it would show the result the user just asked to replace; the
    HTTP routes already answer "not ready" here, and every reader must agree."""
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / default_name).write_text("stale")
    with pytest.raises(RecordingArtifactMissing):
        read_recording_transcript(_Rec(tmp_path), kind)
