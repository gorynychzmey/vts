"""Who owns a task's artifact directory (vts-8w1r).

While a task WAS the recording, deleting the task could always rmtree its
directory — nothing else pointed at those files. A Recording that outlives its
task breaks that assumption: the same directory may now belong to a recording
that is still very much alive.

The rule is deliberately narrow, because the alternative is worse in both
directions:

  * a task deleted with its own recording still cleans up, so the Delete button
    keeps meaning what it says. Pressing Delete and finding the data still
    there would be the more surprising behaviour, not the safer one;
  * a directory claimed by a recording that is NOT this task's — one detached
    by SET NULL, or re-pointed — is left alone. Removing it would destroy a
    live recording's transcript and media, and no confirmation dialog was ever
    shown for that.

Deleting a recording is a separate action from deleting a task; it is that
action's job to remove the artifacts it owns.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import Recording


async def artifacts_removable_for_task(session: AsyncSession, task: Any) -> bool:
    """True when deleting this task may also remove its artifact directory.

    False when some OTHER recording still points at the same directory — the
    detached case SET NULL exists for.
    """
    artifact_dir = getattr(task, "artifact_dir", None)
    if not artifact_dir:
        return False
    task_id = getattr(task, "id", None)
    claimants = (
        await session.execute(
            select(Recording.source_task_id).where(Recording.artifact_dir == artifact_dir)
        )
    ).scalars().all()
    # Nothing claims it (a task predating recordings), or the only claim is this
    # task's own recording, which dies with it.
    return all(claim == task_id for claim in claimants)


async def delete_task_with_recording(session: AsyncSession, task: Any) -> None:
    """Delete a task together with the recording it produced.

    `SET NULL` on `recordings.source_task_id` makes a recording SURVIVE its
    task, which is right for a recording someone detached — and wrong for the
    ordinary Delete button. Without this the deletion left a ghost: a library
    entry whose files had just been removed, which no path could delete
    afterwards because its `source_task_id` was already NULL (vts-t4kg).

    It is not only untidy. `transcript_chunks` cascade from the RECORDING, and
    each chunk holds the full text of its passage — so a "deleted" recording
    kept the transcript in the database, ready to be returned by corpus search.
    A product that stores transcripts of people's conversations cannot leave
    the text behind after a delete.

    Only the task's OWN recording goes. A detached one belongs to nobody and
    stays.
    """
    await session.execute(
        delete(Recording).where(Recording.source_task_id == task.id)
    )
    await session.delete(task)
