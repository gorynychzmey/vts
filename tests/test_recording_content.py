"""Recording content columns and the repo setters that fill them (vts-lzad)."""
from __future__ import annotations

import uuid

from tests.test_recordings import _USER, _task_with_recording, factory  # noqa: F401
from vts.db.repo import Repo


async def test_set_task_content_writes_the_recording_only(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        task.transcript_path = None  # the fixture's own seed; the setter must not refill it
        await Repo(s).set_task_content(
            task,
            transcript_path="/a/outputs/transcript.txt",
            summary_path="/a/summary/final.md",
            redacted_path="/a/outputs/redacted_transcript.txt",
        )
        assert (task.transcript_path, task.summary_path) == (None, None)
        assert rec.summary_path == "/a/summary/final.md"
        assert rec.transcript_path == "/a/outputs/transcript.txt"
        assert rec.redacted_path == "/a/outputs/redacted_transcript.txt"


async def test_unset_fields_are_left_alone(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        original = rec.transcript_path
        repo = Repo(s)
        await repo.set_task_content(task, summary_path="/s")
        assert rec.summary_path == "/s"
        await repo.set_task_content(task, summary_path=None)
        assert rec.summary_path is None
        assert rec.transcript_path == original
        assert rec.redacted_path is None


async def test_prompt_results_reach_the_recording(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        entries = [{"source": "user", "id": "x", "name": "Memo", "path": "/p", "status": "completed"}]
        await Repo(s).set_task_prompt_results(task, entries)
        assert rec.prompt_results == entries
        assert "prompt_results" not in task.options
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


async def test_setters_without_recording_do_not_raise(factory):
    async with factory() as s:
        repo = Repo(s)
        task = await repo.create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={}, artifact_dir="/tmp/none",
        )
        # create_task makes the recording; drop it to model a deleted one.
        await s.delete(await repo.recording_for_task(task.id))
        await s.flush()
        await repo.set_task_content(task, transcript_path="/t", redacted_path="/r")
        await repo.set_task_prompt_results(task, [{"id": "x"}])
        await repo.set_detected_language(task, "russian", 0.5)
        # Nothing to write to: the content is dropped, the runtime input kept.
        assert task.transcript_path is None
        assert "prompt_results" not in task.options
        assert task.options["detected_language"] == "russian"
        assert await repo.recording_for_task(task.id) is None


async def test_create_task_creates_its_recording(factory):
    from vts.db.models import RecordingState
    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v", options={}, artifact_dir="/a",
            source_title="Team sync",
        )
        rec = await Repo(s).recording_for_task(task.id)
        assert rec is not None
        assert (rec.state, rec.artifact_dir, rec.transcript_path) == (RecordingState.processing, "/a", None)
        assert rec.user_id == _USER
        assert rec.title == "Team sync"
        assert rec.source_url == "https://example.com/v"
        assert rec.recorded_at == task.created_at


async def test_new_recording_is_not_in_the_library_until_it_has_text(factory):
    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v", options={}, artifact_dir="/a",
        )
        await s.commit()
        assert await Repo(s).count_recordings(_USER) == 0
        await Repo(s).set_task_content(task, transcript_path="/a/t.txt")
        await s.commit()
        assert await Repo(s).count_recordings(_USER) == 1


async def test_deleting_a_running_task_then_writing_content_is_a_noop(factory):
    """Covers only the recording-lookup guard: the setter runs on a detached object."""
    from vts.api._helpers.recordings import delete_task_with_recording
    from sqlalchemy import select
    from vts.db.models import Recording, Task
    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v", options={}, artifact_dir="/a",
        )
        await s.commit()
    async with factory() as s:
        live = await s.get(Task, task.id)
        await delete_task_with_recording(s, live)
        await s.commit()
    async with factory() as s:
        await Repo(s).set_task_content(task, transcript_path="/x")  # stale object: no raise
        await s.commit()
        assert (await s.scalars(select(Recording))).all() == []


def _pipeline_ctx(factory):
    from vts.pipeline.context import PipelineContext

    ctx = PipelineContext.__new__(PipelineContext)
    ctx.session_factory = factory
    return ctx


async def test_persist_detected_language_reaches_the_recording(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        task_id, rec_id = task.id, rec.id
    await _pipeline_ctx(factory).persist_detected_language(task_id, "german", 0.9)
    async with factory() as s:
        from vts.db.models import Recording, Task
        # The fixture task is created with language "ru": "de" can only come from the write.
        assert (await s.get(Recording, rec_id)).language == "de"
        assert (await s.get(Task, task_id)).options["detected_language"] == "german"


async def test_persist_content_and_prompt_result_reach_the_recording(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        task_id, rec_id = task.id, rec.id
    ctx = _pipeline_ctx(factory)
    await ctx.persist_content(task_id, summary_path="/a/summary/final.md", redacted_path="/a/r.txt")
    await ctx.persist_prompt_result(task_id, "user", "p1", "Memo", "/a/summary/results/p1.md")
    async with factory() as s:
        from vts.db.models import Recording, Task
        rec = await s.get(Recording, rec_id)
        assert (rec.summary_path, rec.redacted_path) == ("/a/summary/final.md", "/a/r.txt")
        assert rec.prompt_results[0]["name"] == "Memo"
        task = await s.get(Task, task_id)
        assert task.summary_path is None
        assert "prompt_results" not in task.options
    # A task deleted mid-run: quiet no-op.
    await ctx.persist_content(uuid.uuid4(), summary_path="/x")


async def test_summary_restart_clears_the_recording_too(factory):
    from vts.db.models import Recording, Task, TaskStatus
    from sqlalchemy.orm import selectinload
    from vts.services.summary_restart import reset_task_for_summary_restart
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        repo = Repo(s)
        task.status = TaskStatus.completed
        await repo.set_task_prompt_results(
            task, [{"source": "system", "id": "summary", "name": "S", "path": "/p", "status": "completed"}]
        )
        await repo.set_task_content(task, summary_path="/a/final.md", redacted_path="/a/r.txt")
        await s.commit()
        task = await s.get(Task, task.id, options=[selectinload(Task.steps)], populate_existing=True)
        await reset_task_for_summary_restart(repo, task)
        assert rec.summary_path is None and rec.redacted_path is None
        assert rec.prompt_results[0]["status"] == "pending"
        assert "prompt_results" not in task.options


async def test_restart_final_with_prompts_empties_recording_results(factory):
    from vts.db.models import Step, StepStatus, Task, TaskStatus
    from sqlalchemy.orm import selectinload
    from vts.services.summary_restart import restart_final_with_prompts
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        repo = Repo(s)
        task.status = TaskStatus.completed
        await repo.set_task_prompt_results(
            task, [{"source": "system", "id": "summary", "name": "S", "path": "/p", "status": "completed"}]
        )
        await repo.set_task_content(task, summary_path="/a/final.md")
        for name in ("download", "summarize_windows", "summarize_final"):
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        task = await s.get(Task, task.id, options=[selectinload(Task.steps)], populate_existing=True)

        class _Bus:
            async def notify_queued(self):
                return None

        await restart_final_with_prompts(repo, task, [{"source": "system", "id": "summary"}], _Bus())
        assert rec.prompt_results == []
        assert rec.summary_path is None


async def test_source_title_discovery_updates_the_early_recording_title(factory):
    from vts.db.models import Recording
    from vts.db.repo import Repo

    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={}, artifact_dir="/tmp/early",
        )
        await s.commit()
        task_id = task.id
    await _pipeline_ctx(factory).save_task_source_title(task_id, "Found title")
    async with factory() as s:
        rec = await Repo(s).recording_for_task(task_id)
        assert rec.title == "Found title"


async def test_source_title_discovery_keeps_a_custom_recording_title(factory):
    from vts.db.repo import Repo

    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={}, artifact_dir="/tmp/early2",
        )
        rec = await Repo(s).recording_for_task(task.id)
        rec.title = "Mine"
        rec.title_is_custom = True
        await s.commit()
        task_id = task.id
    await _pipeline_ctx(factory).save_task_source_title(task_id, "Found title")
    async with factory() as s:
        rec = await Repo(s).recording_for_task(task_id)
        assert rec.title == "Mine"


async def test_persist_prompt_result_builds_on_the_recording_not_stale_task_options(factory):
    """Prod tasks still carry an old `options["prompt_results"]`; nothing may resurrect it."""
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        task.options = {
            **task.options,
            "prompt_results": [{"source": "user", "id": "old", "name": "Old", "path": "/old", "status": "completed"}],
        }
        await s.commit()
        task_id, rec_id = task.id, rec.id
    ctx = _pipeline_ctx(factory)
    await ctx.persist_prompt_result(task_id, "user", "p1", "Memo", "/a/summary/results/p1.md")
    await ctx.persist_prompt_result(task_id, "user", "p2", "Todo", "/a/summary/results/p2.md")
    async with factory() as s:
        from vts.db.models import Recording
        rec = await s.get(Recording, rec_id)
        assert [e["id"] for e in rec.prompt_results] == ["p1", "p2"]


async def test_a_pipeline_shaped_run_leaves_the_task_content_fields_empty(factory):
    """Every writer a run goes through, in order: only the recording is written."""
    from vts.db.models import Recording, Task, TaskStatus
    async with factory() as s:
        task = await Repo(s).create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={"prompts": [{"source": "system", "id": "summary"}]}, artifact_dir="/a",
        )
        await s.commit()
        task_id = task.id
    ctx = _pipeline_ctx(factory)
    await ctx.persist_detected_language(task_id, "russian", 0.8)
    await ctx.persist_content(task_id, transcript_path="/a/outputs/transcript.txt")
    await ctx.persist_content(task_id, redacted_path="/a/outputs/redacted_transcript.txt")
    await ctx.persist_content(task_id, summary_path="/a/summary/final.md")
    await ctx.persist_prompt_result(task_id, "system", "summary", "Summary", "/a/summary/final.md")
    async with factory() as s:
        repo = Repo(s)
        task = await s.get(Task, task_id)
        await repo.upsert_recording_for_task(task)
        await repo.set_task_status(task, TaskStatus.completed)
        await s.commit()
    async with factory() as s:
        task = await s.get(Task, task_id)
        rec = await Repo(s).recording_for_task(task_id)
        assert (task.transcript_path, task.summary_path) == (None, None)
        assert "prompt_results" not in task.options
        assert task.options["detected_language"] == "russian"
        assert rec.transcript_path == "/a/outputs/transcript.txt"
        assert rec.summary_path == "/a/summary/final.md"
        assert rec.redacted_path == "/a/outputs/redacted_transcript.txt"
        assert [e["id"] for e in rec.prompt_results] == ["summary"]
        assert rec.language == "ru"
        assert "prompt_results" not in (rec.meta or {})


async def test_upsert_does_not_copy_content_from_the_task(factory):
    async with factory() as s:
        task, rec = await _task_with_recording(s)
        await Repo(s).set_task_content(task, summary_path=None)
        task.summary_path = "/stale/summary.md"
        task.options = {**task.options, "prompt_results": [{"id": "stale"}]}
        await Repo(s).upsert_recording_for_task(task)
        assert rec.summary_path is None
        assert rec.prompt_results == []
