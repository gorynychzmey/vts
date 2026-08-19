"""Which statuses defer a summary restart to the worker, and which do not.

The deferral exists to dodge one specific race: resetting artefacts under a
step that is still writing them. It is only correct for statuses where a
worker actually holds the task — and getting that set wrong strands the task
rather than merely being suboptimal, because nothing else will ever run the
deferred reset (vts-gouq).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from vts.db.models import Step, StepStatus, Task, TaskStatus


class _FakeRedis:
    """Async Redis stub covering the flag surface RedisBus touches here."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message) -> int:
        self.published.append((channel, message))
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value

    async def set(self, key, value, ex=None) -> bool:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value
        return True

    async def exists(self, key) -> int:
        return 1 if key in self.store else 0

    async def delete(self, key) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


_UID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


async def _seed_task(factory, tmp_path: Path, status: TaskStatus) -> uuid.UUID:
    """A summary-selected task with completed summary steps and real artefacts."""
    art = tmp_path / f"task-{status.value}"
    (art / "summary").mkdir(parents=True)
    (art / "summary" / "final.md").write_text("old summary")
    (art / "summary" / "window_1.txt").write_text("old window")
    async with factory() as s:
        task = Task(
            id=uuid.uuid4(),
            user_id=_UID,
            source_url="x",
            artifact_dir=str(art),
            status=status,
            summary_path=str(art / "summary" / "final.md"),
            options={"prompts": [{"source": "system", "id": "summary"}]},
        )
        s.add(task)
        for name in ("download", "merge_transcript", "summarize_windows", "summarize_final"):
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        return task.id


@pytest.mark.asyncio
async def test_restart_of_a_paused_task_is_done_now_not_deferred(
    client, authed_app, tmp_path
):
    """C1: a paused task must be reset synchronously, or it hangs forever.

    Deferring looks symmetric with `running` and is not. `reap` only iterates
    `WorkerPool._active`, and a paused task is not in it — the coroutine was
    already collected — so `_restart_if_requested` would never be called for
    it. The user would get "restarting", the flag would sit there until its
    TTL quietly expired, and the task would stay paused forever.

    Reverting the fix (deferring `paused` again) makes this fail on the status
    assertion: the task stays `paused` instead of going `queued`.
    """
    app, factory = authed_app
    fake = _FakeRedis()
    app.state.redis = fake
    task_id = await _seed_task(factory, tmp_path, TaskStatus.paused)

    resp = await client.post(f"/api/tasks/{task_id}/restart_summary", json={"mode": "full"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued", (
        "a paused task is held by nobody, so the reset happens now — "
        "'restarting' would promise a worker step that never runs"
    )

    async with factory() as s:
        row = await s.get(Task, task_id)
        assert row.status == TaskStatus.queued
        assert row.summary_path is None

    # And it is genuinely runnable: no flag is left behind for `admit` to trip on.
    from vts.core.config import Settings
    from vts.services.redis_bus import RedisBus

    bus = RedisBus(fake, Settings(redis_url="redis://fake:6379/0"))  # type: ignore[arg-type]
    assert await bus.is_restart_requested(task_id) is False
    assert await bus.is_cancel_requested(task_id) is False
    assert await bus.is_pause_requested(task_id) is False

    # The artefacts really are gone, not merely marked stale.
    art = Path(str((await _reload(factory, task_id)).artifact_dir))
    assert not (art / "summary" / "final.md").exists()
    assert not (art / "summary" / "window_1.txt").exists()


async def _reload(factory, task_id: uuid.UUID) -> Task:
    async with factory() as s:
        return await s.get(Task, task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.running, TaskStatus.waiting])
async def test_restart_of_a_worker_held_task_is_deferred(
    status, client, authed_app, tmp_path
):
    """I1: `waiting` is a running task that lost its GPU slot, so it defers too.

    `waiting` used to be missing from both the gate and the endpoint, which
    made the endpoint answer 409 for a task that is exactly as safe to restart
    as `running` (it sits in `_active` and `reap` will collect it). Reverting
    the gate fix makes the `waiting` case fail with 409.
    """
    app, factory = authed_app
    fake = _FakeRedis()
    app.state.redis = fake
    task_id = await _seed_task(factory, tmp_path, status)

    resp = await client.post(f"/api/tasks/{task_id}/restart_summary", json={"mode": "full"})
    assert resp.status_code == 200, f"{status.value} must be restartable"
    assert resp.json()["status"] == "restarting"

    from vts.core.config import Settings
    from vts.services.redis_bus import RedisBus

    bus = RedisBus(fake, Settings(redis_url="redis://fake:6379/0"))  # type: ignore[arg-type]
    assert await bus.is_restart_requested(task_id) is True
    assert await bus.is_cancel_requested(task_id) is True

    # Nothing was touched yet — that is the whole point of deferring.
    async with factory() as s:
        row = await s.get(Task, task_id)
        assert row.status == status
    art = tmp_path / f"task-{status.value}"
    assert (art / "summary" / "final.md").exists()


@pytest.mark.asyncio
async def test_resume_withdraws_the_cancel_and_restart_flags(
    client, authed_app, tmp_path
):
    """C2: resume used to clear only the pause flag, so it read as a delete.

    `admit` checks the cancel flag before anything else and sends the task
    straight to `canceled` without starting it. A paused task that carries a
    cancel flag — which restart_summary sets, and which any earlier cancel
    could leave behind — therefore died on resume with nothing in the log but
    "skipping canceled task before start".

    Reverting the fix leaves both flags set and this fails on the first two
    assertions.
    """
    app, factory = authed_app
    fake = _FakeRedis()
    app.state.redis = fake
    task_id = await _seed_task(factory, tmp_path, TaskStatus.paused)

    from vts.core.config import Settings
    from vts.services.redis_bus import RedisBus

    bus = RedisBus(fake, Settings(redis_url="redis://fake:6379/0"))  # type: ignore[arg-type]
    await bus.request_pause(task_id)
    await bus.request_cancel(task_id)
    await bus.request_restart(task_id)

    resp = await client.post("/api/tasks/resume", json={"task_ids": [str(task_id)]})
    assert resp.status_code == 200
    assert resp.json()["results"][str(task_id)] == "queued"

    assert await bus.is_cancel_requested(task_id) is False, (
        "a stale cancel flag makes admit discard the task the user just resumed"
    )
    assert await bus.is_restart_requested(task_id) is False, (
        "a stale restart flag makes reap wipe the artefacts of a task the user "
        "asked to carry on with"
    )
    assert await bus.is_pause_requested(task_id) is False
