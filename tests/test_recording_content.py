"""Recording content columns and the repo setters that fill them (vts-lzad)."""
from __future__ import annotations

from tests.test_recordings import _USER, _task_with_recording, factory  # noqa: F401
from vts.db.repo import Repo


async def test_set_task_content_writes_recording_and_task(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        await Repo(s).set_task_content(
            task,
            transcript_path="/a/outputs/transcript.txt",
            redacted_path="/a/outputs/redacted_transcript.txt",
        )
        assert task.transcript_path == "/a/outputs/transcript.txt"
        assert rec.transcript_path == "/a/outputs/transcript.txt"
        assert rec.redacted_path == "/a/outputs/redacted_transcript.txt"


async def test_unset_fields_are_left_alone(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        original = rec.transcript_path
        repo = Repo(s)
        await repo.set_task_content(task, summary_path="/s")
        assert (task.summary_path, rec.summary_path) == ("/s", "/s")
        await repo.set_task_content(task, summary_path=None)
        assert rec.summary_path is None and task.summary_path is None
        assert rec.transcript_path == original and task.transcript_path == original
        assert rec.redacted_path is None


async def test_prompt_results_reach_the_recording(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        entries = [{"source": "user", "id": "x", "name": "Memo", "path": "/p", "status": "completed"}]
        await Repo(s).set_task_prompt_results(task, entries)
        assert rec.prompt_results == entries
        assert task.options["prompt_results"] == entries
        # A copy: later mutation of the caller's dict must not leak in.
        entries[0]["status"] = "failed"
        assert rec.prompt_results[0]["status"] == "completed"


async def test_prompt_results_persist_after_commit(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        await Repo(s).set_task_prompt_results(task, [{"id": "x"}])
        await s.commit()
        rid = rec.id
    async with factory() as s:
        from vts.db.models import Recording
        assert (await s.get(Recording, rid)).prompt_results == [{"id": "x"}]


async def test_detected_language_is_normalised_on_the_recording(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        await Repo(s).set_detected_language(task, "russian", 0.9)
        assert rec.language == "ru"
        assert task.options["detected_language"] == "russian"
        assert task.options["detected_language_confidence"] == 0.9


async def test_mirror_prompt_results_copies_task_options(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        task.options = {**task.options, "prompt_results": [{"id": "a", "status": "completed"}]}
        await Repo(s).mirror_prompt_results(task)
        assert rec.prompt_results == [{"id": "a", "status": "completed"}]


async def test_setters_without_recording_do_not_raise(factory):
    async with factory() as s:
        repo = Repo(s)
        task = await repo.create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={}, artifact_dir="/tmp/none",
        )
        await repo.set_task_content(task, transcript_path="/t", redacted_path="/r")
        await repo.set_task_prompt_results(task, [{"id": "x"}])
        await repo.set_detected_language(task, "russian", 0.5)
        await repo.mirror_prompt_results(task)
        assert task.transcript_path == "/t"
        assert task.options["detected_language"] == "russian"
        assert await repo.recording_for_task(task.id) is None
