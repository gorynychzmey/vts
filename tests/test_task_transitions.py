from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.api.main import can_pause_task, can_restart_final_summary_task, can_restart_summary_task, can_resume_task
from vts.api.schemas import PromptRef, TaskCreateRequest
from vts.db.base import Base
from vts.db.models import StepStatus, Task, TaskStatus
from vts.db.repo import Repo


def test_restart_gates_cannot_see_a_transcriptless_task_with_prompts() -> None:
    """vts-7zi: the restart gates don't model the frontend's `transcript is False`
    short-circuit, and don't need to — but ONLY because the API refuses to create
    the task shape that would expose the difference.

    With transcript=false the pipeline enables just `download`
    (processor.py:_is_step_enabled), so no summary step ever runs. The old JS
    derived summaryExpected from enabledSteps and returned False. The Python gates
    key off the selected prompts instead, so transcript=false + a selected prompt
    WOULD wrongly report can_restart_summary=True. That shape is unreachable:
    prompts and transcript=false is rejected at creation. This test pins that
    coupling — if the validation is ever relaxed, the gates must gain the
    transcript check, and this test is where that shows up.
    """
    with pytest.raises(ValidationError, match="prompts require transcript"):
        TaskCreateRequest(
            url="https://example.com/x",
            transcript=False,
            prompts=[PromptRef(source="system", id="summary")],
        )

    # Without prompts (the only creatable transcript=false shape), both gates are
    # False regardless of status, so the missing short-circuit is unobservable.
    task = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": False, "prompts": []},
        steps=[SimpleNamespace(name="download", status=StepStatus.completed)],
    )
    assert can_restart_summary_task(task) is False
    assert can_restart_final_summary_task(task) is False


def test_can_pause_task_allows_queued_running_or_waiting() -> None:
    assert can_pause_task(TaskStatus.queued)
    assert can_pause_task(TaskStatus.running)
    assert can_pause_task(TaskStatus.waiting)
    assert not can_pause_task(TaskStatus.paused)
    assert not can_pause_task(TaskStatus.completed)
    assert not can_pause_task(TaskStatus.archived)
    assert not can_pause_task(TaskStatus.failed)
    assert not can_pause_task(TaskStatus.canceled)


def test_can_resume_task_allows_paused_or_failed() -> None:
    assert can_resume_task(TaskStatus.paused)
    assert can_resume_task(TaskStatus.failed)
    assert not can_resume_task(TaskStatus.queued)
    assert not can_resume_task(TaskStatus.running)
    assert not can_resume_task(TaskStatus.completed)
    assert not can_resume_task(TaskStatus.archived)
    assert not can_resume_task(TaskStatus.canceled)


def test_can_restart_summary_task_allows_completed_or_summary_failed() -> None:
    completed = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "summary": True},
        steps=[],
    )
    failed_summary = SimpleNamespace(
        status=TaskStatus.failed,
        options={"transcript": True, "summary": True},
        steps=[SimpleNamespace(name="summarize_final", status=StepStatus.failed)],
    )
    failed_non_summary = SimpleNamespace(
        status=TaskStatus.failed,
        options={"transcript": True, "summary": True},
        steps=[SimpleNamespace(name="transcribe_segments", status=StepStatus.failed)],
    )
    completed_without_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "summary": False},
        steps=[],
    )

    assert can_restart_summary_task(completed)
    assert can_restart_summary_task(failed_summary)
    assert not can_restart_summary_task(failed_non_summary)
    assert not can_restart_summary_task(completed_without_summary)


def test_can_restart_summary_task_with_prompts_selection() -> None:
    # New tasks carry a `prompts` list instead of the legacy `summary` bool.
    prompts_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "prompts": [{"source": "system", "id": "summary"}]},
        steps=[],
    )
    prompts_empty = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "prompts": []},
        steps=[],
    )
    legacy_no_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "summary": False},
        steps=[],
    )

    assert can_restart_summary_task(prompts_summary)
    assert not can_restart_summary_task(prompts_empty)
    assert not can_restart_summary_task(legacy_no_summary)


def test_can_restart_final_summary_task() -> None:
    windows_ok = SimpleNamespace(name="summarize_windows", status=StepStatus.completed)
    final_failed = SimpleNamespace(name="summarize_final", status=StepStatus.failed)
    final_ok = SimpleNamespace(name="summarize_final", status=StepStatus.completed)

    completed = SimpleNamespace(
        status=TaskStatus.completed,
        options={"summary": True},
        steps=[windows_ok, final_ok],
    )
    failed_final = SimpleNamespace(
        status=TaskStatus.failed,
        options={"summary": True},
        steps=[windows_ok, final_failed],
    )
    windows_not_done = SimpleNamespace(
        status=TaskStatus.failed,
        options={"summary": True},
        steps=[SimpleNamespace(name="summarize_windows", status=StepStatus.failed), final_failed],
    )
    # no_summary still returns False — but now via the windows-not-done path
    # (steps=[] means summarize_windows is not completed), NOT because a summary
    # prompt is required. The loosened gate no longer checks for a summary ref.
    no_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"summary": False},
        steps=[],
    )
    # NEW semantics: a custom-only set (no summary prompt) with windows done and
    # the task completed is now restartable — the gate no longer requires summary.
    custom_only = SimpleNamespace(
        status=TaskStatus.completed,
        options={"prompts": [{"source": "user", "id": "a"}]},
        steps=[windows_ok],  # windows done, no summarize_final present
    )

    assert can_restart_final_summary_task(completed)
    assert can_restart_final_summary_task(failed_final)
    assert not can_restart_final_summary_task(windows_not_done)
    assert not can_restart_final_summary_task(no_summary)
    assert can_restart_final_summary_task(custom_only)  # NEW: gate no longer requires summary


def test_can_restart_summary_task_allows_failed_pack_window_notes() -> None:
    # C1: pack_window_notes is a real summary step (vts/pipeline/types.py) and the
    # frontend's SUMMARY_STEPS (app.js on main) included it. A task that failed in
    # pack_window_notes is a recoverable summary failure and must stay restartable.
    failed_pack = SimpleNamespace(
        status=TaskStatus.failed,
        options={"transcript": True, "prompts": [{"source": "system", "id": "summary"}]},
        steps=[SimpleNamespace(name="pack_window_notes", status=StepStatus.failed)],
    )
    # Control: an unrelated failed step must NOT enable the summary restart.
    failed_unrelated = SimpleNamespace(
        status=TaskStatus.failed,
        options={"transcript": True, "prompts": [{"source": "system", "id": "summary"}]},
        steps=[SimpleNamespace(name="extract_audio", status=StepStatus.failed)],
    )

    assert can_restart_summary_task(failed_pack)
    assert not can_restart_summary_task(failed_unrelated)


def test_can_restart_final_summary_task_requires_a_selected_prompt() -> None:
    # I1: mirrors the frontend's `summaryExpected` gate (app.js:1174 on main) —
    # restarting the final summary requires at least one selected prompt.
    windows_ok = SimpleNamespace(name="summarize_windows", status=StepStatus.completed)

    # No prompt selected at all, but windows completed: the frontend disabled the
    # button here. Without the refs gate this wrongly returns True.
    no_prompts_legacy = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "summary": False},
        steps=[windows_ok],
    )
    no_prompts_list = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "prompts": []},
        steps=[windows_ok],
    )
    # Control: WITH the system summary prompt selected -> still True.
    with_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "prompts": [{"source": "system", "id": "summary"}]},
        steps=[windows_ok],
    )
    # The gate is "any prompt selected", NOT "the system summary prompt selected":
    # a user-prompt-only task produces a `finalize:user:<id>` step, which the
    # frontend's summaryExpected counted. It must stay restartable.
    user_prompt_only = SimpleNamespace(
        status=TaskStatus.completed,
        options={"transcript": True, "prompts": [{"source": "user", "id": "42"}]},
        steps=[windows_ok],
    )

    assert not can_restart_final_summary_task(no_prompts_legacy)
    assert not can_restart_final_summary_task(no_prompts_list)
    assert can_restart_final_summary_task(with_summary)
    assert can_restart_final_summary_task(user_prompt_only)


def test_waiting_status_exists():
    from vts.db.models import TaskStatus

    assert TaskStatus.waiting.value == "waiting"


def test_summary_stages_is_subset_of_summary_step_names():
    # Drift guard for C1 (SUMMARY_STEP_NAMES missing `pack_window_notes` after it was
    # added to the pipeline). There are two independent copies of "which steps belong
    # to the summary pipeline":
    #   - vts.api.main.SUMMARY_STEP_NAMES: the *restartable* summary steps used to
    #     gate can_restart_summary_task (any step in this set that failed => the
    #     summary phase can be restarted).
    #   - vts.mcp.tools._SUMMARY_STAGES: the steps whose *progress* is reported via
    #     summary_progress_for_task (current/total window counts) rather than ASR
    #     progress or no progress at all.
    # These are NOT required to be equal: _SUMMARY_STAGES intentionally excludes the
    # prep steps `prepare_llama_model` and `prepare_summary_chunks` because those
    # steps don't have a meaningful window current/total to report yet (progress
    # only becomes countable once window summarization starts). So the true
    # relationship is _SUMMARY_STAGES ⊆ SUMMARY_STEP_NAMES, not equality. Assert the
    # subset relationship, and print the symmetric difference on failure so a future
    # drift (e.g. a renamed/removed step) names the exact offending step.
    from vts.api.main import SUMMARY_STEP_NAMES
    from vts.mcp.tools import _SUMMARY_STAGES

    missing_from_step_names = _SUMMARY_STAGES - SUMMARY_STEP_NAMES
    assert not missing_from_step_names, (
        "_SUMMARY_STAGES (vts/mcp/tools.py) has stages not present in "
        f"SUMMARY_STEP_NAMES (vts/api/main.py): {sorted(missing_from_step_names)}. "
        "Either SUMMARY_STEP_NAMES is missing a step (like C1's pack_window_notes) "
        "or _SUMMARY_STAGES references a stage that no longer exists."
    )


def test_summary_step_names_are_real_pipeline_steps():
    # Guard against a typo'd or removed step name in SUMMARY_STEP_NAMES: every name
    # in it must actually appear in the pipeline's real step list. `summarize_final`
    # is the one exception — it's a finalize/tail step appended per selected prompt
    # by build_dag_steps(), not part of the static DAG_HEAD, so it's covered via
    # DAG_STEPS (DAG_HEAD + "summarize_final") instead of DAG_HEAD alone.
    from vts.api.main import SUMMARY_STEP_NAMES
    from vts.pipeline.types import DAG_STEPS

    unknown_steps = SUMMARY_STEP_NAMES - set(DAG_STEPS)
    assert not unknown_steps, (
        "SUMMARY_STEP_NAMES (vts/api/main.py) references step names that don't "
        f"exist in the real pipeline step list DAG_STEPS (vts/pipeline/types.py): "
        f"{sorted(unknown_steps)}. Symmetric difference with DAG_STEPS: "
        f"{sorted(SUMMARY_STEP_NAMES ^ set(DAG_STEPS))}."
    )


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as sess:
            yield sess
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_requeue_running_tasks_includes_waiting(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("requeue@example.com")
    await session.flush()

    waiting_task = Task(
        user_id=user.id, source_url="u1", status=TaskStatus.waiting,
        options={}, artifact_dir="/tmp/waiting",
    )
    running_task = Task(
        user_id=user.id, source_url="u2", status=TaskStatus.running,
        options={}, artifact_dir="/tmp/running",
    )
    queued_task = Task(
        user_id=user.id, source_url="u3", status=TaskStatus.queued,
        options={}, artifact_dir="/tmp/queued",
    )
    session.add_all([waiting_task, running_task, queued_task])
    await session.commit()

    requeued_ids = await repo.requeue_running_tasks()
    await session.commit()

    assert set(requeued_ids) == {waiting_task.id, running_task.id}

    await session.refresh(waiting_task)
    await session.refresh(running_task)
    await session.refresh(queued_task)
    assert waiting_task.status == TaskStatus.queued
    assert running_task.status == TaskStatus.queued
    assert queued_task.status == TaskStatus.queued


@pytest.mark.asyncio
async def test_transition_task_status_running_to_waiting(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("trans@example.com")
    await session.flush()
    task = Task(
        user_id=user.id, source_url="u1", status=TaskStatus.running,
        options={}, artifact_dir="/tmp/t1",
    )
    session.add(task)
    await session.commit()

    changed = await repo.transition_task_status(
        task.id, [TaskStatus.running], TaskStatus.waiting
    )
    await session.commit()

    assert changed is True
    await session.refresh(task)
    assert task.status == TaskStatus.waiting


@pytest.mark.asyncio
async def test_transition_task_status_noop_on_canceled(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("trans2@example.com")
    await session.flush()
    task = Task(
        user_id=user.id, source_url="u2", status=TaskStatus.canceled,
        options={}, artifact_dir="/tmp/t2",
    )
    session.add(task)
    await session.commit()

    changed = await repo.transition_task_status(
        task.id, [TaskStatus.running], TaskStatus.waiting
    )
    await session.commit()

    assert changed is False
    await session.refresh(task)
    assert task.status == TaskStatus.canceled
