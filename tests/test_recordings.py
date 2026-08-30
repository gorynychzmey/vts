"""Recording: the lasting object a task produces (vts-8w1r / VOS-130).

Until now the task WAS the recording — deleting a task deleted the transcript,
the media and the segments with it, because nothing else claimed them. That was
consistent while "a task is a recording"; it stops being consistent the moment
a recording is supposed to outlive the run that made it.

The mechanism follows the precedent already in the schema: voice_samples
outlive their task through source_task_id ... ondelete=SET NULL. A recording
does the same, and additionally keeps its own artifact_dir, so the files stay
where they are and ownership of the directory passes to the recording.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, Task, TaskStatus, User
from vts.db.repo import Repo
from vts.api._helpers.recordings import delete_task_with_recording

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


async def _task_with_recording(session, artifact_dir="/tmp/rec"):
    repo = Repo(session)
    task = await repo.create_task(
        user_id=_USER, source_url="https://example.com/v",
        options={"language": "ru"}, artifact_dir=artifact_dir,
    )
    task.source_title = "Team sync"
    recording = await repo.upsert_recording_for_task(task)
    await session.commit()
    return task, recording


@pytest.mark.asyncio
async def test_a_task_produces_a_recording(factory):
    async with factory() as session:
        task, recording = await _task_with_recording(session)
        assert recording.source_task_id == task.id
        assert recording.user_id == _USER
        assert recording.title == "Team sync"
        assert recording.artifact_dir == task.artifact_dir


@pytest.mark.asyncio
async def test_the_recording_outlives_its_task(factory):
    # The whole point: deleting the task must leave the recording standing,
    # with its own pointer to the artifacts.
    async with factory() as session:
        task, recording = await _task_with_recording(session)
        recording_id, artifact_dir = recording.id, recording.artifact_dir
        await session.delete(task)
        await session.commit()

    async with factory() as session:
        survivor = await session.get(Recording, recording_id)
        assert survivor is not None, "the recording died with its task"
        assert survivor.source_task_id is None, "the dangling task reference was not cleared"
        assert survivor.artifact_dir == artifact_dir, "the recording lost its artifacts"


@pytest.mark.asyncio
async def test_re_running_a_task_updates_its_recording_rather_than_adding_one(factory):
    # A task creates OR UPDATES its recording; it does not accumulate them.
    async with factory() as session:
        task, first = await _task_with_recording(session)
        repo = Repo(session)
        task.source_title = "Team sync (renamed)"
        task.transcript_path = "/tmp/rec/outputs/transcript.txt"
        second = await repo.upsert_recording_for_task(task)
        await session.commit()

        assert second.id == first.id
        assert second.title == "Team sync (renamed)"
        assert second.transcript_path == "/tmp/rec/outputs/transcript.txt"
        rows = (await session.execute(
            select(Recording).where(Recording.source_task_id == task.id)
        )).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_duration_and_language_survive_without_the_media(factory):
    # Duration used to be probed from the media file and language read out of
    # Task.options; archiving deletes the media, so both had to become columns.
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={"language": "de"}, artifact_dir="/tmp/rec",
        )
        await repo.add_asr_segment(
            task_id=task.id, segment_index=0, start_sec=0.0, end_sec=300.0,
            text="a", raw_json={},
        )
        await repo.add_asr_segment(
            task_id=task.id, segment_index=1, start_sec=300.0, end_sec=612.5,
            text="b", raw_json={},
        )
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()

        # The last segment's end IS the length — and unlike an ffprobe of the
        # media file, it is still there after the media is gone.
        assert recording.duration_sec == 612.5
        assert recording.language == "de"


@pytest.mark.asyncio
async def test_a_recording_with_no_segments_states_no_duration(factory):
    # Better an absent value than a fabricated zero: a task whose media never
    # arrived has no length to report.
    async with factory() as session:
        _task, recording = await _task_with_recording(session)
        assert recording.duration_sec is None


@pytest.mark.asyncio
async def test_deleting_the_user_still_removes_their_recordings(factory):
    # SET NULL is about the TASK, not the user: a deleted account must not
    # leave its recordings behind.
    async with factory() as session:
        _task, recording = await _task_with_recording(session)
        recording_id = recording.id
        # Deleted in SQL, not through the ORM: session.delete() would try to
        # null out tasks.user_id itself rather than letting the database
        # cascade, and it is the DATABASE-level cascade under test here.
        await session.execute(
            text("DELETE FROM users WHERE id = :uid"), {"uid": _USER}
        )
        await session.commit()

    async with factory() as session:
        assert await session.get(Recording, recording_id) is None


@pytest.mark.asyncio
async def test_listing_recordings_is_scoped_to_the_user(factory):
    async with factory() as session:
        other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
        session.add(User(id=other, username="someone-else"))
        await session.flush()
        repo = Repo(session)
        await _task_with_recording(session)
        foreign = await repo.create_task(
            user_id=other, source_url="https://example.com/other",
            options={}, artifact_dir="/tmp/other",
        )
        await repo.upsert_recording_for_task(foreign)
        await session.commit()

        mine = await repo.list_recordings(_USER)
        assert len(mine) == 1
        assert all(r.user_id == _USER for r in mine)


# ------------------------------------------------- deleting: task vs recording

@pytest.mark.asyncio
async def test_deleting_a_task_removes_its_recording_and_artifacts(factory, tmp_path):
    """The existing Delete button must keep meaning what it says.

    A recording that outlives its task is the point of this feature, but not at
    the cost of turning "delete" into "delete some of it". A user deleting a
    task from the list is deleting that recording too — the alternative is
    pressing Delete and finding the data still there, which is the more
    surprising of the two behaviours.

    Keeping the artifacts is for a recording that has been DETACHED from its
    task (the next test), not for every deletion.
    """
    from vts.api._helpers.recordings import artifacts_removable_for_task

    artifact_dir = tmp_path / "task-dir"
    artifact_dir.mkdir()
    (artifact_dir / "keep.txt").write_text("data")

    async with factory() as session:
        task, _rec = await _task_with_recording(session, artifact_dir=str(artifact_dir))
        removable = await artifacts_removable_for_task(session, task)
        assert removable is True, "a task whose recording is its own must still clean up"


@pytest.mark.asyncio
async def test_a_detached_recording_keeps_its_artifacts(factory, tmp_path):
    """A recording no longer tied to this task owns the directory.

    This is the case the SET NULL exists for: the task is gone (or the
    recording was re-pointed), so removing the directory would destroy a live
    recording's transcript and media.
    """
    from vts.api._helpers.recordings import artifacts_removable_for_task

    artifact_dir = tmp_path / "shared-dir"
    artifact_dir.mkdir()

    async with factory() as session:
        task, recording = await _task_with_recording(session, artifact_dir=str(artifact_dir))
        # Detach: the recording stays, pointing at the same directory.
        recording.source_task_id = None
        await session.commit()

        removable = await artifacts_removable_for_task(session, task)
        assert removable is False, (
            "deleting the task would have removed a directory a live recording owns"
        )


# --------------------------------------------------------------- library API

@pytest.mark.asyncio
async def test_library_lists_the_users_recordings(authed_app, client, tmp_path):
    from tests.conftest import _TEST_USER_ID

    _app, session_factory = authed_app
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    transcript = outputs / "transcript.txt"
    transcript.write_text("hello", encoding="utf-8")

    async with session_factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID), source_url="https://example.com/v",
            options={"language": "ru"}, artifact_dir=str(tmp_path),
        )
        task.source_title = "Team sync"
        task.transcript_path = str(transcript)
        await repo.add_asr_segment(
            task_id=task.id, segment_index=0, start_sec=0.0, end_sec=42.5,
            text="hello", raw_json={},
        )
        await repo.upsert_recording_for_task(task)
        await session.commit()

    r = await client.get("/api/recordings")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["title"] == "Team sync"
    assert item["language"] == "ru"
    assert item["duration_sec"] == 42.5
    assert item["has_transcript"] is True
    # No media on disk in this fixture — the flag must say so rather than
    # assuming a recording always has its media.
    assert item["has_media"] is False


@pytest.mark.asyncio
async def test_library_hides_another_users_recording(authed_app, client, tmp_path):
    from vts.db.models import User

    _app, session_factory = authed_app
    other = uuid.uuid4()
    async with session_factory() as session:
        session.add(User(id=other, username="someone-else"))
        await session.flush()
        repo = Repo(session)
        task = await repo.create_task(
            user_id=other, source_url="https://example.com/secret",
            options={}, artifact_dir=str(tmp_path),
        )
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        foreign_id = recording.id

    listing = await client.get("/api/recordings")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0

    # And it is not reachable by id either — the library is not a new way
    # around the owner check.
    direct = await client.get(f"/api/recordings/{foreign_id}")
    assert direct.status_code == 404


@pytest.mark.asyncio
async def test_a_recording_survives_its_task_in_the_library(authed_app, client, tmp_path):
    from tests.conftest import _TEST_USER_ID

    _app, session_factory = authed_app
    async with session_factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID), source_url="https://example.com/v",
            options={}, artifact_dir=str(tmp_path),
        )
        task.source_title = "Kept"
        await repo.upsert_recording_for_task(task)
        await session.commit()
        await session.delete(task)
        await session.commit()

    r = await client.get("/api/recordings")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["total"] == 1, "the recording vanished with its task"
    assert payload["items"][0]["title"] == "Kept"
    assert payload["items"][0]["source_task_id"] is None


# ------------------------------------------------------- language, normalised

@pytest.mark.parametrize("stored,expected", [
    ("ru", "ru"),
    ("russian", "ru"),      # what the cpp backend writes
    ("Russian", "ru"),
    ("english", "en"),
    ("en", "en"),
    ("de", "de"),
    ("german", "de"),
    ("", None),
    (None, None),
    ("klingon", "klingon"), # unknown: kept as-is rather than dropped
])
def test_language_code_normalises_backend_spellings(stored, expected):
    """Production carries BOTH spellings for the same language.

    Measured on a restored copy: 104 recordings say "russian" and 1 says "ru",
    because the ASR sidecar returns a language CODE while the cpp backend
    returns the full English name. That is a pre-existing property of the
    stored options, not something the library introduced — but a library that
    lists "russian" and "ru" as two different languages is wrong on its face.

    Normalising on write keeps the fix inside this feature instead of
    rewriting a pipeline that has its own reasons for what it stores. Unknown
    values pass through untouched: guessing would be worse than showing what
    is actually there.
    """
    from vts.services.recording_meta import language_code

    assert language_code(stored) == expected


def test_migration_language_map_matches_the_python_one():
    """The backfill maps languages in SQL; this module maps them in Python.

    Two implementations of one mapping drift silently — the migration would
    keep writing "russian" for rows the application would have written "ru"
    for. Rather than trust that they were kept in step, read the CASE arms out
    of the migration and compare.
    """
    import re
    from pathlib import Path

    from vts.services.recording_meta import _NAME_TO_CODE

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0027_recordings.py"
    ).read_text(encoding="utf-8")
    arms = dict(re.findall(r"WHEN '([a-z]+)'\s+THEN '([a-z]{2})'", migration))
    assert arms, "no CASE arms found — did the migration's shape change?"
    assert arms == _NAME_TO_CODE, (
        "the migration's language map and recording_meta._NAME_TO_CODE disagree: "
        f"only in SQL={set(arms) - set(_NAME_TO_CODE)}, "
        f"only in Python={set(_NAME_TO_CODE) - set(arms)}"
    )


# ------------------------------------- deletion must not leave a ghost behind

@pytest.mark.asyncio
async def test_deleting_a_task_deletes_its_recording_row_too(factory):
    """The docstring above says a task deletion deletes its recording. Prove it.

    The earlier test only asserted that the ARTIFACTS were removable — which
    they were — and never checked the row. So the intent was documented, tested
    in appearance, and absent in fact (vts-t4kg): SET NULL detached the
    recording instead, leaving a library entry whose files were gone and which
    no path could delete afterwards, because `source_task_id` was already NULL.

    It matters beyond tidiness: transcript_chunks cascade from the RECORDING,
    and each chunk holds the full text of its passage. A "deleted" recording
    would keep the transcript in the database and, once corpus search is wired
    up, keep answering searches with it.
    """
    from vts.db.models import TranscriptChunk

    async with factory() as session:
        task, recording = await _task_with_recording(session)
        recording_id = recording.id
        session.add(TranscriptChunk(
            recording_id=recording_id, user_id=_USER, chunk_index=0,
            text="что было сказано в этой записи", start_sec=0.0, end_sec=5.0,
            speakers=[], embedding=None, embedding_model=None,
        ))
        await session.commit()

        await delete_task_with_recording(session, task)
        await session.commit()

    async with factory() as session:
        assert await session.get(Recording, recording_id) is None, (
            "the recording survived its task as an undeletable ghost"
        )
        chunks = (await session.execute(select(TranscriptChunk))).scalars().all()
        assert chunks == [], "the transcript text outlived the deletion"


@pytest.mark.asyncio
async def test_deleting_a_task_leaves_a_detached_recording_alone(factory):
    """Only the task's OWN recording goes. A detached one is a separate object.

    This is the case SET NULL exists for, and the reason deletion cannot simply
    cascade at the database level.
    """
    async with factory() as session:
        task, recording = await _task_with_recording(session)
        recording.source_task_id = None
        await session.commit()
        kept_id = recording.id

        await delete_task_with_recording(session, task)
        await session.commit()

    async with factory() as session:
        assert await session.get(Recording, kept_id) is not None, (
            "deleting a task destroyed a recording that no longer belonged to it"
        )


# --------------------------------------------- a recording knows its own name

@pytest.mark.asyncio
async def test_a_recording_falls_back_to_the_source_when_the_task_is_untitled(factory):
    """A recording must carry a usable name, not an empty one.

    Measured on production: 55 of 122 recordings had no title — not because
    anything was lost, but because their TASK had none either. An upload only
    gets a title if the user types one, and the name was sitting in
    `source_url` all along (`file://19.05.2026 22.10.m4a`).

    Deriving it in the browser would be wrong twice over: the name is a
    property of the recording, not of one rendering of it, and it has to be
    the same string everywhere — the list, the API, an MCP client, a future
    export.
    """
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://19.05.2026 22.10.m4a",
            options={}, artifact_dir="/tmp/x",
        )
        task.source_title = None
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert recording.title == "19.05.2026 22.10.m4a", (
            "an uploaded recording did not take its filename as a name"
        )


@pytest.mark.asyncio
async def test_a_url_task_without_a_title_uses_the_url(factory):
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="https://example.com/watch?v=abc",
            options={}, artifact_dir="/tmp/x",
        )
        task.source_title = None
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert recording.title == "https://example.com/watch?v=abc"


@pytest.mark.asyncio
async def test_an_explicit_title_always_wins(factory):
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://recording.m4a",
            options={}, artifact_dir="/tmp/x",
        )
        task.source_title = "Планёрка 12 марта"
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert recording.title == "Планёрка 12 марта"


@pytest.mark.asyncio
async def test_renaming_a_task_renames_its_recording(factory):
    """The name has to stay in step, or the two views disagree about one thing.

    Renaming is a task action, but the recording is what the library shows —
    so a rename that stopped at the task would leave the library displaying the
    old name with no way to fix it.
    """
    from vts.api._helpers.recordings import rename_recording_for_task

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://old.m4a", options={},
            artifact_dir="/tmp/x",
        )
        task.source_title = "Old name"
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert recording.title == "Old name"

        task.source_title = "New name"
        await rename_recording_for_task(session, task)
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(Recording))).scalars().first()
        assert stored.title == "New name"


@pytest.mark.asyncio
async def test_clearing_a_title_falls_back_rather_than_blanking(factory):
    # Emptying the field means "use the default name", not "have no name".
    from vts.api._helpers.recordings import rename_recording_for_task

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://clip.m4a", options={},
            artifact_dir="/tmp/x",
        )
        task.source_title = "Something"
        await repo.upsert_recording_for_task(task)
        await session.commit()

        task.source_title = None
        await rename_recording_for_task(session, task)
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(Recording))).scalars().first()
        assert stored.title == "clip.m4a"


# ------------------------------------- a recording's own name is its own

@pytest.mark.asyncio
async def test_a_recording_renamed_by_hand_keeps_its_name(factory):
    """Once someone names a RECORDING, the task stops speaking for it.

    A recording outlives its task and is the object the library is about, so
    its name has to be independently editable. Without a marker for "someone
    chose this", the next task rename — or the next pipeline run, which upserts
    the recording — would silently overwrite a deliberate name with a derived
    one. That is data loss the user cannot see coming.
    """
    from vts.api._helpers.recordings import (
        rename_recording,
        rename_recording_for_task,
    )

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://raw.m4a", options={},
            artifact_dir="/tmp/x",
        )
        task.source_title = "Task name"
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert recording.title == "Task name"

        await rename_recording(session, recording, "Планёрка по релизу")
        await session.commit()
        assert recording.title == "Планёрка по релизу"

        # The task is renamed afterwards; the recording must not follow.
        task.source_title = "Task renamed again"
        await rename_recording_for_task(session, task)
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(Recording))).scalars().first()
        assert stored.title == "Планёрка по релизу", (
            "a task rename overwrote a name the user gave the recording"
        )


@pytest.mark.asyncio
async def test_a_pipeline_rerun_does_not_overwrite_a_chosen_name(factory):
    # upsert_recording_for_task runs again on every re-index and at task
    # completion — the most likely way a deliberate name would disappear.
    from vts.api._helpers.recordings import rename_recording

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://raw.m4a", options={},
            artifact_dir="/tmp/x",
        )
        task.source_title = "Task name"
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()

        await rename_recording(session, recording, "Мой заголовок")
        await session.commit()

        again = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert again.title == "Мой заголовок", (
            "re-running the pipeline reset the recording's name"
        )


@pytest.mark.asyncio
async def test_clearing_a_recording_name_returns_it_to_following_the_task(factory):
    """Emptying the field means "go back to the default", not "be nameless".

    It is also the only way out: without it, naming a recording once would lock
    it away from its task forever.
    """
    from vts.api._helpers.recordings import (
        rename_recording,
        rename_recording_for_task,
    )

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://raw.m4a", options={},
            artifact_dir="/tmp/x",
        )
        task.source_title = "Task name"
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()

        await rename_recording(session, recording, "Своё имя")
        await session.commit()
        await rename_recording(session, recording, "   ")
        await session.commit()
        assert recording.title == "Task name", "clearing did not restore the derived name"

        task.source_title = "Task renamed"
        await rename_recording_for_task(session, task)
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(Recording))).scalars().first()
        assert stored.title == "Task renamed", (
            "the recording did not resume following its task after being cleared"
        )
