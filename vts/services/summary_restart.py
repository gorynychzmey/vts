"""Resetting a task back to a fresh full-summary run.

Two callers need exactly this sequence: the API endpoint, for a task nobody is
holding (completed / failed / paused), and the worker's reap hook, for a task
it has just released. They used to carry a line-by-line copy each, and the two
copies had already drifted in the one place it matters — the order of the DB
mutation and the irreversible file deletion. Living here, one function, both
get the safe order and neither can drift again (vts-gouq).

This module sits in ``services`` rather than ``api/_helpers`` on purpose: the
worker has no business importing the API layer to reach it.
"""

from __future__ import annotations

import asyncio

from vts.api._helpers.artifact_store import _reset_summary_artifacts, _reset_summary_steps
from vts.db.models import Task, TaskStatus
from vts.db.repo import Repo
from vts.services.prompt_results import downgrade_all_result_entries

# Statuses in which a WorkerPool coroutine is actually holding the task, i.e.
# the task is in `WorkerPool._active` and `reap` will eventually see it. Only
# for these is a restart deferred to the worker; anything else must be reset
# synchronously, or the deferred reset would never run.
#
# `waiting` belongs here because it is a *running* task that lost its GPU slot
# (running->waiting->running, see pipeline/context.py mark_waiting): the
# coroutine is alive and still writing artefacts, so it races exactly like
# `running` does.
#
# `paused` deliberately does NOT belong here. A paused task has been removed
# from `_active`, so nothing would ever reap it and run the deferred reset —
# and with no worker holding it there is no race to defer around anyway.
WORKER_HELD_STATUSES = (TaskStatus.running, TaskStatus.waiting)


async def reset_task_for_summary_restart(repo: Repo, task: Task) -> None:
    """Re-queue ``task`` for a full summary re-run: DB first, files after.

    The order is the point. ``_reset_summary_artifacts`` deletes files and
    cannot be undone, while everything before the commit can — so a commit that
    fails after the files were already gone would roll the task back to
    ``completed`` with a ``summary_path`` pointing at a file that no longer
    exists, and the UI would 404 on it. That is the same defect class as
    vts-b6l. Committing first inverts the worst case into a self-healing one: a
    task sitting in ``queued`` with stale files that the next run overwrites
    anyway.

    This commits the session itself rather than leaving it to the caller: the
    ordering only holds if the commit and the deletion stay together, and both
    callers previously got that wrong in their own copy.
    """
    _reset_summary_steps(task)
    downgrade_all_result_entries(task)
    task.summary_path = None
    await repo.set_task_summary_progress(task, 0, 0)
    await repo.set_task_status(task, TaskStatus.queued)
    await repo.session.commit()
    # Committed: from here on the DB says "queued", so losing the process
    # mid-deletion leaves stale files for the next run to overwrite rather
    # than a completed task whose artefacts are gone.
    await asyncio.to_thread(_reset_summary_artifacts, task)
