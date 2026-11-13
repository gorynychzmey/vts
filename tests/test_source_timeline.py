"""Timecodes must be in the SOURCE media's timeline, not the trimmed audio's.

trim_initial_silence cuts leading silence off the WAV that ASR and diarization
read, so their times start where the speech starts. The player, search hits and
subtitles seek in the untrimmed media. Nothing used to add the cut back, so on a
recording with 9.3 s of silence every sentence played 9.3 s before it was
spoken (vts-kb4z; 55 of 123 production recordings were off by over a second).

These tests pin the two halves of the fix: new work is written shifted, and the
data migration brings old artifacts and rows onto the same timeline exactly
once.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import AsrSegment, Recording, Task, TaskStatus, TranscriptChunk, User
from vts.services.source_timeline import (
    OFFSET_KEY,
    migrate_artifact_dir,
    migrate_old_timelines,
    shift_spans,
    source_offset,
)

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000b7")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifacts(root: Path, trimmed: float | None) -> Path:
    """An artifact dir as a pre-fix pipeline left it: times start at 0."""
    if trimmed is not None:
        _write(root / "outputs" / "audio_preprocess.json", {"trimmed_seconds": trimmed})
    entries = [
        {"start": 0.0, "end": 4.0, "text": "Hello", "speaker": "SPEAKER_00"},
        {"start": 4.0, "end": 9.5, "text": "Hi", "speaker": "SPEAKER_01"},
    ]
    _write(root / "outputs" / "transcript.json", {
        "text": "Hello Hi", "entries": entries, "all_entries": entries, "cleanup": {},
    })
    _write(root / "asr" / "segments_raw.json", {"segments": [
        {"segment_index": 1, "start": 0.0, "end": 9.5,
         "raw_json": {"segments": [{"start": 0.0, "end": 4.0, "text": "Hello"}]}},
    ]})
    _write(root / "outputs" / "diarization.json", {
        "segments": [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
            {"start": 4.0, "end": 9.5, "speaker": "SPEAKER_01"},
        ],
        "embeddings": {"SPEAKER_00": [0.1]},
        "num_speakers": 2,
    })
    return root


# ---------------------------------------------------------------- the offset


def test_offset_is_what_the_trim_step_recorded(tmp_path: Path) -> None:
    _write(tmp_path / "audio_preprocess.json", {"trimmed_seconds": 9.329})
    assert source_offset(tmp_path) == 9.329


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"trimmed_seconds": "x"}',
                                     '{"trimmed_seconds": -1}'])
def test_an_unknown_offset_is_zero(tmp_path: Path, content: str | None) -> None:
    """No marker means nothing was trimmed, or nothing can be known: shifting
    by a guess would move correct times."""
    if content is not None:
        (tmp_path / "audio_preprocess.json").write_text(content, encoding="utf-8")
    assert source_offset(tmp_path) == 0.0


def test_shift_spans_moves_start_and_end_and_keeps_the_rest() -> None:
    spans = [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}, {"start": 4.0, "end": 9.5}]
    assert shift_spans(spans, 2.5) == [
        {"start": 2.5, "end": 6.5, "speaker": "SPEAKER_00"},
        {"start": 6.5, "end": 12.0},
    ]
    # The input is not touched: callers keep the working-audio times too.
    assert spans[0]["start"] == 0.0


# ------------------------------------------------------------- the artifacts


def test_migrating_shifts_every_absolute_time(tmp_path: Path) -> None:
    root = _artifacts(tmp_path / "t", 9.329)

    assert migrate_artifact_dir(root) == pytest.approx(9.329)

    transcript = _read(root / "outputs" / "transcript.json")
    assert [(e["start"], e["end"]) for e in transcript["entries"]] == [
        (pytest.approx(9.329), pytest.approx(13.329)),
        (pytest.approx(13.329), pytest.approx(18.829)),
    ]
    assert transcript["all_entries"] == transcript["entries"]
    assert transcript["entries"][0]["speaker"] == "SPEAKER_00"

    raw = _read(root / "asr" / "segments_raw.json")["segments"][0]
    assert (raw["start"], raw["end"]) == (pytest.approx(9.329), pytest.approx(18.829))
    # Inner sentence times are LOCAL to the chunk; shifting the chunk shifts
    # them. Moving them as well would double the offset.
    assert raw["raw_json"]["segments"][0]["start"] == 0.0

    diar = _read(root / "outputs" / "diarization.json")
    assert [(s["start"], s["end"]) for s in diar["segments"]] == [
        (pytest.approx(9.329), pytest.approx(13.329)),
        (pytest.approx(13.329), pytest.approx(18.829)),
    ]
    assert diar["embeddings"] == {"SPEAKER_00": [0.1]}


def test_migrating_twice_shifts_once(tmp_path: Path) -> None:
    """A migration that failed half-way is run again: every file records the
    offset it carries, so a second pass must not move it further."""
    root = _artifacts(tmp_path / "t", 9.329)
    migrate_artifact_dir(root)
    migrate_artifact_dir(root)

    assert _read(root / "outputs" / "transcript.json")["entries"][0]["start"] == pytest.approx(9.329)
    assert _read(root / "outputs" / "transcript.json")[OFFSET_KEY] == pytest.approx(9.329)
    assert _read(root / "outputs" / "diarization.json")["segments"][0]["start"] == pytest.approx(9.329)
    assert _read(root / "asr" / "segments_raw.json")["segments"][0]["start"] == pytest.approx(9.329)


@pytest.mark.parametrize("trimmed", [None, 0.0])
def test_nothing_trimmed_nothing_moved(tmp_path: Path, trimmed: float | None) -> None:
    root = _artifacts(tmp_path / "t", trimmed)
    before = _read(root / "outputs" / "transcript.json")

    assert migrate_artifact_dir(root) == 0.0
    assert _read(root / "outputs" / "transcript.json") == before


def test_missing_files_are_skipped(tmp_path: Path) -> None:
    """An undiarized task has no diarization.json; a task cut short has no
    transcript yet. Whatever exists is moved, the rest is not invented."""
    root = tmp_path / "t"
    _write(root / "outputs" / "audio_preprocess.json", {"trimmed_seconds": 3.0})
    _write(root / "asr" / "segments_raw.json", {"segments": [{"start": 0.0, "end": 5.0}]})

    assert migrate_artifact_dir(root) == 3.0
    assert _read(root / "asr" / "segments_raw.json")["segments"][0]["start"] == 3.0
    assert not (root / "outputs" / "transcript.json").exists()
    assert not (root / "outputs" / "diarization.json").exists()


# ------------------------------------------------------------- the database


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


async def _seed(factory, artifact_dir: Path) -> tuple[uuid.UUID, uuid.UUID]:
    task_id, recording_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Task(id=task_id, user_id=_USER, source_url="https://example.com/v",
                   status=TaskStatus.completed, options={}, artifact_dir=str(artifact_dir)))
        await s.flush()
        s.add(AsrSegment(task_id=task_id, segment_index=1, start_sec=0.0, end_sec=9.5,
                         text="Hello Hi"))
        s.add(Recording(id=recording_id, user_id=_USER, source_task_id=task_id,
                        artifact_dir=str(artifact_dir)))
        await s.flush()
        s.add(TranscriptChunk(recording_id=recording_id, user_id=_USER, chunk_index=0,
                              text="Hello Hi", start_sec=0.0, end_sec=9.5, speakers=[]))
        await s.commit()
    return task_id, recording_id


async def _times(factory) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    async with factory() as s:
        segs = (await s.execute(text("SELECT start_sec, end_sec FROM asr_segments"))).all()
        chunks = (await s.execute(text("SELECT start_sec, end_sec FROM transcript_chunks"))).all()
    return [tuple(r) for r in segs], [tuple(r) for r in chunks]


async def _migrate(factory) -> int:
    async with factory() as s:
        conn = await s.connection()
        moved = await conn.run_sync(migrate_old_timelines)
        await s.commit()
    return moved


async def test_migration_shifts_segments_chunks_and_files_together(factory, tmp_path: Path) -> None:
    root = _artifacts(tmp_path / "t", 9.329)
    await _seed(factory, root)

    assert await _migrate(factory) == 1

    segs, chunks = await _times(factory)
    assert segs == [(pytest.approx(9.329), pytest.approx(18.829))]
    assert chunks == [(pytest.approx(9.329), pytest.approx(18.829))]
    assert _read(root / "outputs" / "transcript.json")["entries"][0]["start"] == pytest.approx(9.329)


async def test_rows_are_left_alone_when_the_offset_is_unknown(factory, tmp_path: Path) -> None:
    """Artifacts not reachable from where the migration runs (no data volume
    mounted) or never trimmed: the rows stay as they are, consistent with the
    files that also stay as they are."""
    await _seed(factory, tmp_path / "absent")

    assert await _migrate(factory) == 0
    assert await _times(factory) == ([(0.0, 9.5)], [(0.0, 9.5)])


async def test_a_directory_shared_by_task_and_recording_moves_once(factory, tmp_path: Path) -> None:
    """The recording inherits its task's artifact_dir: the directory is one
    unit of work, not one per row that points at it."""
    root = _artifacts(tmp_path / "t", 2.0)
    await _seed(factory, root)

    await _migrate(factory)

    assert _read(root / "outputs" / "transcript.json")["entries"][0]["start"] == 2.0
    assert await _times(factory) == ([(2.0, 11.5)], [(2.0, 11.5)])


# ------------------------------------------------------- new work, shifted


async def test_asr_segments_are_stored_on_the_source_timeline(tmp_path: Path, monkeypatch) -> None:
    """A chunk cut at 0..10 s of the trimmed audio was spoken at 2.5..12.5 s
    of the media. The row carries the media's time; the player, the merge and
    the search index all read it from there."""
    import logging
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from vts.pipeline.steps.base import StepState
    from vts.pipeline.steps.transcription import TranscribeSegmentsStep

    stored: list[dict] = []

    class _Repo:
        def __init__(self, session) -> None:
            pass

        async def get_task_segments(self, task_id):
            return []

        async def upsert_asr_segment_payload(self, **kwargs):
            stored.append(kwargs)

        async def get_task_by_id(self, task_id):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self) -> None:
            return None

    class _Whisper:
        backend_name = "fake"

        async def transcribe(self, **kwargs):
            return {"text": "Hello there", "segments": [{"start": 0.0, "end": 2.0, "text": "Hello there"}]}

        def normalize_output(self, raw):
            return raw["text"]

    class _Bus:
        async def publish_event(self, **kwargs) -> None:
            return None

    @asynccontextmanager
    async def _slot(*args):
        yield

    async def _not_paused(task_id) -> None:
        return None

    monkeypatch.setattr("vts.pipeline.steps.transcription.Repo", _Repo)
    root = tmp_path / "task"
    dirs = {name: root / name for name in ("outputs", "segments", "media")}
    dirs["root"] = root
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    _write(dirs["outputs"] / "audio_preprocess.json", {"trimmed_seconds": 2.5})
    _write(dirs["outputs"] / "segments_manifest.json", {"segments": [
        {"segment_index": 1, "start": 0.0, "end": 10.0, "file": "0001.wav"},
    ]})
    (dirs["segments"] / "0001.wav").write_bytes(b"wav")

    ctx = SimpleNamespace(
        session_factory=_Session,
        whisper=_Whisper(),
        bus=_Bus(),
        gpu_slot=_slot,
        check_paused=_not_paused,
        get_emitter=lambda task_id: None,
        settings=SimpleNamespace(services_database_write_throttle_ms=0),
    )
    st = StepState(task_id=uuid.uuid4(), user_id="u", dirs=dirs,
                   logger=logging.getLogger("test"), task_options={"language": "en"})

    await TranscribeSegmentsStep().run(ctx, st)

    assert [(s["start_sec"], s["end_sec"]) for s in stored] == [(2.5, 12.5)]


def test_the_migration_refuses_to_downgrade() -> None:
    """A no-op downgrade followed by an upgrade would move the rows twice:
    they carry no marker, only the files do."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0031_source_timeline.py"
    spec = importlib.util.spec_from_file_location("migration_0031", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(RuntimeError, match="shift them twice"):
        module.downgrade()
