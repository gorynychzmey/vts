from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import signal
import time
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import get_settings
from vts.core.logging import configure_logging
from sqlalchemy.orm import selectinload

from vts.db.models import Task, TaskStatus
from vts.services.summary_restart import reset_task_for_summary_restart
from vts.db.repo import Repo
from vts.db.session import SessionLocal
from vts.delivery.consumer import delivery_loop
from vts.pipeline.processor import TaskProcessor
from vts.services.redis_bus import RedisBus
from vts.services.step_weights_recompute import recompute_all_users
from vts.services.upload_session import delete_abandoned_sessions, find_abandoned_sessions
from vts.worker.lanes import LaneManager


# How long a pause request waits for the step to stop itself before the pool
# interrupts it with atask.cancel().
#
# Only the step can free the hardware: yt-dlp and the diarization sidecar are
# child processes, and atask.cancel() unwinds the awaiting coroutine without
# touching them. Both steps poll the pause flag from their progress callback
# and kill the child themselves, but download throttles that Redis lookup to
# _CANCEL_POLL_INTERVAL_S = 1.0s and acts on the answer one tick later, so it
# needs up to ~2s. Cancelling immediately therefore won all but a sliver of
# the races and the child survived the pause. Three seconds covers the two
# second worst case plus the kill and unwind, and is still far below the tick
# budget of anything a user would notice.
#
# Cancel is deliberately NOT graced: a canceled task is discarded, so there is
# no cooperative shutdown worth waiting for.
_PAUSE_GRACE_S = 3.0


async def recover_pending_tasks(log: logging.Logger) -> list[uuid.UUID]:
    async with SessionLocal() as session:
        repo = Repo(session)
        recovered_running = await repo.requeue_running_tasks()
        await session.commit()
    if recovered_running:
        log.info("recovered running tasks: %s", len(recovered_running))
    return recovered_running


async def reconcile_diarization_jobs(processor: TaskProcessor, log: logging.Logger) -> None:
    """Cancel every diarization job the sidecar is still holding at startup.

    This runs right after recover_pending_tasks, which has just moved every
    in-flight task back to `queued`. That leaves no task in a state that owns a
    running job, so every job the sidecar still lists is orphaned: its result is
    headed for a task that will be re-run from scratch (or one deleted while the
    worker was down). The caller runs this before subscribing to the work
    queue, so no re-attaching run has POSTed a fresh job yet — the job cancelled
    here is always the pre-restart one.

    The idle TTL would eventually reap these, but that burns up to a full TTL of
    CPU. Best-effort: an optimisation over the TTL, never a boot blocker, so any
    failure is logged and swallowed here rather than relying on the callee.
    """
    try:
        job_ids = await processor.diarization.list_jobs()
        for job_id in job_ids:
            await processor.diarization.cancel(job_id)
    except Exception:  # noqa: BLE001 - reconciliation must never break startup
        log.warning("diarization reconciliation failed", exc_info=True)
        return
    if job_ids:
        log.info("cancelled %d orphaned diarization job(s) on startup", len(job_ids))


async def _step_weights_tick(*, min_samples: int) -> None:
    await recompute_all_users(SessionLocal, min_samples=min_samples)


async def _step_weights_loop() -> None:
    settings = get_settings()
    log = logging.getLogger("vts.worker")
    # Small startup jitter so a fresh deploy doesn't recompute before the
    # queue has drained; then recompute on the configured interval.
    await asyncio.sleep(5)
    while True:
        try:
            await _step_weights_tick(min_samples=settings.progress_weights_min_samples)
        except Exception:
            log.exception("step-weights loop iteration failed")
        await asyncio.sleep(settings.progress_weights_recompute_interval_seconds)


async def _upload_gc_tick(*, artifacts_root, ttl_seconds: int) -> list[uuid.UUID]:
    """Delete uploads abandoned before finalize (vts-ee3).

    Scanning and unlinking are blocking, so they run in a thread; the Task-row
    check is one query for the whole sweep rather than one per directory.
    """
    candidates = await asyncio.to_thread(
        find_abandoned_sessions, artifacts_root, ttl_seconds=ttl_seconds
    )
    if not candidates:
        return []
    async with SessionLocal() as session:
        live = await Repo(session).task_ids_in(list(candidates))
    return await asyncio.to_thread(
        delete_abandoned_sessions, candidates, has_task=live.__contains__
    )


async def _upload_gc_loop() -> None:
    settings = get_settings()
    log = logging.getLogger("vts.worker")
    await asyncio.sleep(5)
    while True:
        try:
            removed = await _upload_gc_tick(
                artifacts_root=settings.artifacts_root,
                ttl_seconds=settings.upload_session_ttl_seconds,
            )
            if removed:
                log.info("upload-gc: removed %s abandoned session(s)", len(removed))
        except Exception:
            log.exception("upload-gc loop iteration failed")
        await asyncio.sleep(settings.upload_gc_interval_seconds)


async def _publish_lane_snapshot(redis: Redis, prefix: str, snapshot: dict[str, list[str]]) -> None:
    # Best-effort cache (10s TTL): a transient Redis failure here must never
    # propagate into LaneManager's slot bookkeeping, so swallow and log.
    try:
        await redis.setex(f"{prefix}queue:lanes", 10, json.dumps(snapshot))
    except Exception:
        logging.getLogger("vts.worker").warning(
            "failed to publish lane snapshot", exc_info=True
        )


class WorkerPool:
    """Runs several tasks concurrently, up to ``max_active``.

    Owns a dict of in-flight asyncio Tasks keyed by task id and drives their
    lifecycle in three cooperating phases the loop calls each tick:

    * ``admit``   — dequeue queued tasks up to remaining capacity and spawn
      ``processor.process_task`` coroutines. Skips (and marks canceled) any
      task that already has a cancel request before it starts.
    * ``watch_cancels`` — cancel the asyncio Task of any active task whose id
      has a cancel request, once. A pause request first gets a grace window
      (``_PAUSE_GRACE_S``) in which the step may stop itself.
    * ``reap`` — collect finished coroutines, log the outcome, and clear the
      cancel flag and internal bookkeeping.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        bus: Any,
        processor: Any,
        max_active: int,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._processor = processor
        self._max_active = max(int(max_active), 1)
        self._active: dict[uuid.UUID, asyncio.Task] = {}
        self._cancel_sent: set[uuid.UUID] = set()
        # task_id -> monotonic deadline after which a pending pause stops
        # being cooperative and is enforced with atask.cancel().
        self._pause_deadline: dict[uuid.UUID, float] = {}
        self._log = logging.getLogger("vts.worker")

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def admit(self) -> bool:
        """Dequeue up to remaining capacity and spawn coroutines.

        Returns True if at least one task was admitted (spawned)."""
        admitted = False
        while len(self._active) < self._max_active:
            async with self._session_factory() as session:
                repo = Repo(session)
                task_id = await repo.dequeue_task()
                await session.commit()

            if task_id is None:
                break

            if await self._bus.is_cancel_requested(task_id):
                await self._bus.clear_cancel_request(task_id)
                self._log.info("skipping canceled task %s before start", task_id)
                async with self._session_factory() as session:
                    repo = Repo(session)
                    await repo.set_task_status_by_id(task_id, TaskStatus.canceled)
                    await session.commit()
                continue

            if await self._bus.is_pause_requested(task_id):
                # Symmetric with the cancel check above, for the same reason
                # watch_cancels now honours pause: admitting a task whose pause
                # is still pending starts a step that the very next
                # watch_cancels tick interrupts. Nothing clears the flag on
                # that path, so the task is re-queued and the whole cycle
                # repeats on every restart until the flag's TTL expires.
                await self._bus.clear_pause_request(task_id)
                self._log.info("pausing task %s before start", task_id)
                async with self._session_factory() as session:
                    repo = Repo(session)
                    await repo.set_task_status_by_id(task_id, TaskStatus.paused)
                    await session.commit()
                continue

            await self._bus.clear_cancel_request(task_id)
            self._active[task_id] = asyncio.create_task(
                self._processor.process_task(task_id)
            )
            admitted = True
            self._log.info("processing task %s", task_id)

        return admitted

    async def watch_cancels(self) -> None:
        """Interrupt any active task whose cancel or pause was requested.

        Pause used to be cooperative — the pipeline consulted `check_paused`
        between steps — which left a task generating a single large window
        deaf to the request for as long as that window took. Measured on
        production 2026-08-19: a task reported `paused` while the model kept
        the GPU for over an hour. A pause that does not free the GPU is not a
        pause, so it interrupts the same way a cancel does.

        The difference is what survives: a cancel discards the task, while a
        pause keeps every step already finished (steps guard themselves with
        `already_done`), losing only the window in flight.

        Interrupting is not the same as stopping, though. The long steps run
        their real work in a child process (yt-dlp, the diarization sidecar),
        and `atask.cancel()` unwinds only the coroutine awaiting it — the child
        keeps running, and keeps the hardware. Only the step itself can kill
        it, which it does by polling the pause flag from its progress callback.
        So a pause gets a grace window first: the deadline is recorded on the
        tick that notices the request, and the forced cancel fires on a later
        tick only if the task has not finished on its own by then. The window
        is a deadline rather than a sleep because this method runs inside the
        worker loop; blocking here would stall every other task.

        A cancel keeps interrupting immediately: the task is being discarded,
        so there is no cooperative shutdown worth waiting for.
        """
        now = time.monotonic()
        for task_id, atask in list(self._active.items()):
            if task_id in self._cancel_sent:
                continue
            if await self._bus.is_cancel_requested(task_id):
                self._log.info("cancel requested for running task %s", task_id)
                atask.cancel()
                self._cancel_sent.add(task_id)
                continue
            if not await self._bus.is_pause_requested(task_id):
                # The flag is gone — the pause was withdrawn mid-window. Drop
                # the deadline so a later pause gets a full window of its own
                # rather than inheriting an already-expired one and being
                # enforced on the tick that sees it.
                self._pause_deadline.pop(task_id, None)
                continue
            deadline = self._pause_deadline.get(task_id)
            if deadline is None:
                self._log.info(
                    "pause requested for running task %s; giving it %.1fs to stop itself",
                    task_id,
                    _PAUSE_GRACE_S,
                )
                self._pause_deadline[task_id] = now + _PAUSE_GRACE_S
                continue
            if now < deadline:
                continue
            self._log.info(
                "pause grace expired for task %s; interrupting it", task_id
            )
            atask.cancel()
            self._cancel_sent.add(task_id)

    async def reap(self) -> None:
        """Collect finished coroutines, log outcomes, clear bookkeeping."""
        for task_id, atask in list(self._active.items()):
            if not atask.done():
                continue
            try:
                await atask
            except asyncio.CancelledError:
                self._log.info("task %s canceled", task_id)
            except Exception:
                self._log.exception("task %s crashed with unhandled exception", task_id)
            finally:
                await self._bus.clear_cancel_request(task_id)
                self._active.pop(task_id, None)
                self._cancel_sent.discard(task_id)
                self._pause_deadline.pop(task_id, None)
                await self._restart_if_requested(task_id)

    async def _restart_if_requested(self, task_id: uuid.UUID) -> None:
        """Reset a task's summary stages if a restart was asked for.

        The API cannot do this itself while a worker holds the task: it would
        reset the artefacts under the very step still writing to them, and the
        step would then finish and write its window back over the cleared
        state. Here the task is provably nobody's — it has just been reaped —
        so the reset is safe.

        Failures are logged and swallowed rather than raised: letting the error
        escape would abort the reap loop and strand every other finished task.
        The flag is deliberately left set on that path, so the next reap tick
        retries — clearing it in a `finally` would have made the docstring's
        promise ("can be restarted again") false, since nothing would be left
        to trigger the retry and the user's request would vanish silently. The
        flag's TTL still bounds the retrying.
        """
        if not await self._bus.is_restart_requested(task_id):
            return
        try:
            async with self._session_factory() as session:
                repo = Repo(session)
                # Eager-load `steps`: _reset_summary_steps walks the relation,
                # and lazy-loading it later — after the greenlet context this
                # `get` runs in is gone — raises MissingGreenlet under the
                # async session. _reset_summary_artifacts does NOT touch it (it
                # reads task.artifact_dir, a plain column) and runs in a thread,
                # where a lazy load would raise inside the worker thread and be
                # swallowed; eager-loading here covers that too if it ever
                # grows a relation access.
                task = await session.get(
                    Task, task_id, options=[selectinload(Task.steps)]
                )
                if task is None:
                    self._log.info("restart requested for a task that is gone: %s", task_id)
                    await self._bus.clear_restart_request(task_id)
                    return
                await reset_task_for_summary_restart(repo, task)
            await self._bus.clear_restart_request(task_id)
            await self._bus.notify_queued()
            self._log.info("summary restarted for task %s", task_id)
        except Exception:
            self._log.exception(
                "restart failed for task %s; leaving the flag set to retry", task_id
            )

    async def cancel_all(self) -> None:
        """Cancel every active task and await it (teardown)."""
        for atask in list(self._active.values()):
            if not atask.done():
                atask.cancel()
        for atask in list(self._active.values()):
            with suppress(BaseException):
                await atask
        self._active.clear()
        self._cancel_sent.clear()
        self._pause_deadline.clear()


async def worker_loop() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    bus = RedisBus(redis, settings)
    lanes = LaneManager(
        settings,
        on_change=lambda snap: _publish_lane_snapshot(redis, settings.redis_prefix, snap),
    )
    processor = TaskProcessor(
        session_factory=SessionLocal, redis=redis, settings=settings, lanes=lanes
    )
    log = logging.getLogger("vts.worker")
    notify_channel = f"{settings.redis_prefix}queue:notify"

    pump_task: asyncio.Task[None] | None = None
    weights_task: asyncio.Task[None] | None = None
    upload_gc_task: asyncio.Task[None] | None = None
    delivery_task: asyncio.Task[None] | None = None
    pubsub = None
    pool = WorkerPool(
        session_factory=SessionLocal,
        bus=bus,
        processor=processor,
        max_active=settings.worker_max_active_tasks,
    )

    try:
        await recover_pending_tasks(log)
        # Before subscribing to the work queue: the requeued tasks have not been
        # picked up yet, so any job the sidecar still holds is the pre-restart
        # one and safe to cancel. Doing this after subscription could race a
        # re-attaching run that has already POSTed a fresh job under the same id.
        await reconcile_diarization_jobs(processor, log)

        pubsub = redis.pubsub()
        await pubsub.subscribe(notify_channel)
        wakeup = asyncio.Event()

        async def _pump() -> None:
            async for _ in pubsub.listen():
                wakeup.set()

        pump_task = asyncio.create_task(_pump())

        if settings.progress_weights_enabled:
            weights_task = asyncio.create_task(_step_weights_loop())

        if settings.upload_gc_enabled:
            upload_gc_task = asyncio.create_task(_upload_gc_loop())

        delivery_task = asyncio.create_task(
            delivery_loop(SessionLocal, settings, redis)
        )

        while True:
            admitted = await pool.admit()
            await pool.watch_cancels()
            await pool.reap()
            if not admitted and pool.active_count == 0:
                wakeup.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=5.0)
            else:
                await asyncio.sleep(0.2)
    finally:
        await pool.cancel_all()
        if pump_task is not None:
            pump_task.cancel()
            with suppress(BaseException):
                await pump_task
        if weights_task is not None:
            weights_task.cancel()
            with suppress(asyncio.CancelledError):
                await weights_task
        if upload_gc_task is not None:
            upload_gc_task.cancel()
            with suppress(asyncio.CancelledError):
                await upload_gc_task
        if delivery_task is not None:
            delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await delivery_task
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.unsubscribe(notify_channel)
                await pubsub.aclose()
        await redis.aclose()


async def _run_worker() -> None:
    """Run worker_loop until it finishes or a termination signal arrives.

    The worker is PID 1 in its container, and the kernel applies no default
    action to signals for PID 1 — without an explicit handler SIGTERM is simply
    dropped. That is why stopping the container used to wait out the whole
    timeout and end in SIGKILL, losing the teardown entirely (vts-9er).

    Cancelling the task is all that is needed: worker_loop's own `finally`
    already cancels the pool and every background loop and closes redis.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker_loop())
    log = logging.getLogger("vts.worker")
    installed: list[int] = []

    def _request_stop(signame: str) -> None:
        if not task.done():
            log.info("received %s, shutting down", signame)
            task.cancel()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop, signame)
        except NotImplementedError:
            # Not available on every platform; the container is Linux, so this
            # only guards exotic dev environments.
            continue
        installed.append(sig)

    try:
        await task
    except asyncio.CancelledError:
        # Expected: our own cancellation, not a failure.
        log.info("worker stopped")
    finally:
        # Production exits right after this, but the test suite keeps the loop
        # alive — a handler left behind would fire during an unrelated test.
        for sig in installed:
            loop.remove_signal_handler(sig)


def main() -> None:
    configure_logging()
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
