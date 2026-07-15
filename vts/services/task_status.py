"""Single source of task-status semantics. Pure functions over TaskStatus.

Each set encodes EXACTLY a status group used elsewhere in the codebase (see the
vts-c2n spec). Do NOT unify the three different "terminal" sets — they answer
different questions and any behavior change belongs in a separate issue.
"""
from __future__ import annotations

from vts.db.models import TaskStatus

ACTIVE_STATUSES = {TaskStatus.running, TaskStatus.waiting}
PENDING_STATUSES = {TaskStatus.queued, TaskStatus.waiting}
FINISHED_STATUSES = {
    TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled, TaskStatus.archived,
}
PAUSABLE_STATUSES = {TaskStatus.queued, TaskStatus.running, TaskStatus.waiting}
RESUMABLE_STATUSES = {TaskStatus.paused, TaskStatus.failed}
ARCHIVABLE_STATUSES = {TaskStatus.completed, TaskStatus.failed}
SKIPPABLE_ON_START_STATUSES = {TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}
TERMINAL_FOR_WAIT_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled}


def is_active(status: TaskStatus) -> bool:
    return status in ACTIVE_STATUSES


def is_pending(status: TaskStatus) -> bool:
    return status in PENDING_STATUSES


def is_finished(status: TaskStatus) -> bool:
    return status in FINISHED_STATUSES


def shows_progress(status: TaskStatus) -> bool:
    return is_active(status) or status in {TaskStatus.completed, TaskStatus.failed}


def can_pause(status: TaskStatus) -> bool:
    return status in PAUSABLE_STATUSES


def can_resume(status: TaskStatus) -> bool:
    return status in RESUMABLE_STATUSES


def can_archive(status: TaskStatus) -> bool:
    return status in ARCHIVABLE_STATUSES


def is_skippable_on_start(status: TaskStatus) -> bool:
    return status in SKIPPABLE_ON_START_STATUSES


def is_terminal_for_wait(status: TaskStatus) -> bool:
    return status in TERMINAL_FOR_WAIT_STATUSES


def status_flags() -> dict[str, dict[str, bool]]:
    """Pure-status flags for the frontend, delivered once at bootstrap."""
    return {
        s.value: {
            "is_active": is_active(s),
            "is_pending": is_pending(s),
            "is_finished": is_finished(s),
            "shows_progress": shows_progress(s),
            "can_pause": can_pause(s),
            "can_resume": can_resume(s),
            "can_archive": can_archive(s),
        }
        for s in TaskStatus
    }
