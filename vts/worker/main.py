from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import get_settings
from vts.core.logging import configure_logging
from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.db.session import SessionLocal
from vts.pipeline.processor import TaskProcessor
from vts.services.redis_bus import RedisBus
from vts.services.step_weights_recompute import recompute_all_users
from vts.services.upload_session import delete_abandoned_sessions, find_abandoned_sessions
from vts.worker.lanes import LaneManager


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
      has a cancel request, once.
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

            await self._bus.clear_cancel_request(task_id)
            self._active[task_id] = asyncio.create_task(
                self._processor.process_task(task_id)
            )
            admitted = True
            self._log.info("processing task %s", task_id)

        return admitted

    async def watch_cancels(self) -> None:
        """Cancel the asyncio task of any active task with a cancel request."""
        for task_id, atask in list(self._active.items()):
            if task_id in self._cancel_sent:
                continue
            if await self._bus.is_cancel_requested(task_id):
                self._log.info("cancel requested for running task %s", task_id)
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
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.unsubscribe(notify_channel)
                await pubsub.aclose()
        await redis.aclose()


def main() -> None:
    configure_logging()
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
