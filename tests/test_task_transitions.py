from vts.api.main import can_pause_task, can_resume_task
from vts.db.models import TaskStatus


def test_can_pause_task_allows_only_queued_or_running() -> None:
    assert can_pause_task(TaskStatus.queued)
    assert can_pause_task(TaskStatus.running)
    assert not can_pause_task(TaskStatus.paused)
    assert not can_pause_task(TaskStatus.completed)
    assert not can_pause_task(TaskStatus.failed)
    assert not can_pause_task(TaskStatus.canceled)


def test_can_resume_task_allows_paused_or_failed() -> None:
    assert can_resume_task(TaskStatus.paused)
    assert can_resume_task(TaskStatus.failed)
    assert not can_resume_task(TaskStatus.queued)
    assert not can_resume_task(TaskStatus.running)
    assert not can_resume_task(TaskStatus.completed)
    assert not can_resume_task(TaskStatus.canceled)
