from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from redis.asyncio import Redis

from vts.core.config import get_settings
from vts.core.logging import configure_logging
from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.db.session import SessionLocal
from vts.pipeline.processor import TaskProcessor
from vts.services.redis_bus import RedisBus


async def recover_pending_tasks(bus: RedisBus, log: logging.Logger) -> None:
    async with SessionLocal() as session:
        repo = Repo(session)
        recovered_running = await repo.requeue_running_tasks()
        queued_ids = await repo.list_task_ids_for_statuses([TaskStatus.queued])
        await session.commit()
    for task_id in queued_ids:
        await bus.enqueue_task(task_id)
    if recovered_running:
        log.info("recovered running tasks: %s", len(recovered_running))
    if queued_ids:
        log.info("queued tasks restored on startup: %s", len(queued_ids))


async def worker_loop() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    bus = RedisBus(redis, settings)
    processor = TaskProcessor(session_factory=SessionLocal, redis=redis, settings=settings)
    log = logging.getLogger("vts.worker")
    heavy_slot_key = f"{settings.redis_prefix}heavy_slots"

    try:
        await redis.set(heavy_slot_key, 0)
        log.info("heavy slot counter reset on startup")
        await recover_pending_tasks(bus, log)
        running_task_id = None
        running_task: asyncio.Task[None] | None = None
        cancel_sent = False
        while True:
            if running_task is None:
                task_id = await bus.dequeue_task(timeout_seconds=5)
                if task_id is None:
                    await asyncio.sleep(0.2)
                    continue
                if await bus.is_cancel_requested(task_id):
                    await bus.clear_cancel_request(task_id)
                    log.info("skipping canceled task %s before start", task_id)
                    continue
                await bus.clear_cancel_request(task_id)
                running_task_id = task_id
                running_task = asyncio.create_task(processor.process_task(task_id))
                cancel_sent = False
                log.info("processing task %s", task_id)
                continue

            if running_task_id is not None and not cancel_sent and await bus.is_cancel_requested(running_task_id):
                log.info("cancel requested for running task %s", running_task_id)
                running_task.cancel()
                cancel_sent = True

            if not running_task.done():
                await asyncio.sleep(0.2)
                continue

            try:
                await running_task
            except asyncio.CancelledError:
                if running_task_id is not None:
                    log.info("task %s canceled", running_task_id)
            except Exception:
                if running_task_id is not None:
                    log.exception("task %s crashed with unhandled exception", running_task_id)
            finally:
                if running_task_id is not None:
                    await bus.clear_cancel_request(running_task_id)
                running_task_id = None
                running_task = None
                cancel_sent = False
    finally:
        if "running_task" in locals() and running_task is not None and not running_task.done():
            running_task.cancel()
            with suppress(BaseException):
                await running_task
        await redis.aclose()


def main() -> None:
    configure_logging()
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
