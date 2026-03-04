from types import SimpleNamespace

from vts.api.main import can_pause_task, can_restart_final_summary_task, can_restart_summary_task, can_resume_task
from vts.db.models import StepStatus, TaskStatus


def test_can_pause_task_allows_only_queued_or_running() -> None:
    assert can_pause_task(TaskStatus.queued)
    assert can_pause_task(TaskStatus.running)
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
    no_summary = SimpleNamespace(
        status=TaskStatus.completed,
        options={"summary": False},
        steps=[],
    )

    assert can_restart_final_summary_task(completed)
    assert can_restart_final_summary_task(failed_final)
    assert not can_restart_final_summary_task(windows_not_done)
    assert not can_restart_final_summary_task(no_summary)
