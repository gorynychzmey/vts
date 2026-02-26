from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

from vts.core.config import get_settings
from vts.core.logging import configure_logging
from vts.db.session import SessionLocal
from vts.pipeline.processor import TaskProcessor
from vts.services.redis_bus import RedisBus


async def worker_loop() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    bus = RedisBus(redis, settings)
    processor = TaskProcessor(session_factory=SessionLocal, redis=redis, settings=settings)
    log = logging.getLogger("vts.worker")

    try:
        while True:
            task_id = await bus.dequeue_task(timeout_seconds=5)
            if task_id is None:
                await asyncio.sleep(0.2)
                continue
            log.info("processing task %s", task_id)
            await processor.process_task(task_id)
    finally:
        await redis.aclose()


def main() -> None:
    configure_logging()
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
