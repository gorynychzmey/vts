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


async def recover_pending_tasks(log: logging.Logger) -> None:
    async with SessionLocal() as session:
        repo = Repo(session)
        recovered_running = await repo.requeue_running_tasks()
        await session.commit()
    if recovered_running:
        log.info("recovered running tasks: %s", len(recovered_running))


async def worker_loop() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    bus = RedisBus(redis, settings)
    processor = TaskProcessor(session_factory=SessionLocal, redis=redis, settings=settings)
    log = logging.getLogger("vts.worker")
    heavy_slot_key = f"{settings.redis_prefix}heavy_slots"
    notify_channel = f"{settings.redis_prefix}queue:notify"

    try:
        await redis.set(heavy_slot_key, 0)
        log.info("heavy slot counter reset on startup")
        await recover_pending_tasks(log)

        pubsub = redis.pubsub()
        await pubsub.subscribe(notify_channel)

        running_task_id = None
        running_task: asyncio.Task[None] | None = None
        cancel_sent = False

        while True:
            if running_task is None:
                # Try to claim a queued task from Postgres
                async with SessionLocal() as session:
                    repo = Repo(session)
                    task_id = await repo.dequeue_task()
                    await session.commit()

                if task_id is None:
                    # Nothing queued — wait for a notify or poll every 5s
                    try:
                        await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True, timeout=5), timeout=5.5)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    continue

                if await bus.is_cancel_requested(task_id):
                    await bus.clear_cancel_request(task_id)
                    log.info("skipping canceled task %s before start", task_id)
                    # Mark it canceled in DB
                    async with SessionLocal() as session:
                        repo = Repo(session)
                        task = await repo.get_task_by_id(task_id)
                        if task is not None:
                            await repo.set_task_status(task, TaskStatus.canceled)
                        await session.commit()
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
        with suppress(Exception):
            await pubsub.unsubscribe(notify_channel)
            await pubsub.aclose()
        await redis.aclose()


def main() -> None:
    configure_logging()
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
