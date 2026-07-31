"""Delivery consumer: claim due deliveries, run the adapter, record the outcome.

Runs as a background loop in the worker (see vts/worker/main.py). Claiming and
executing are split across sessions on purpose: claim() flips rows to
`delivering` and commits so a second worker skips them (FOR UPDATE SKIP LOCKED),
then each row is processed in its OWN session — one delivery blowing up can
never roll back another's status write.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

from vts.core.secrets import decrypt_secrets, load_secrets_key
from vts.db.models import DeliveryAttempt, utcnow
from vts.db.repo import Repo
from vts.delivery.contract import DeliveryTargetConfig
from vts.delivery.queue import backoff_seconds
from vts.delivery.registry import UnknownAdapter, get_adapter
from vts.delivery.resolve import resolve_variant

logger = logging.getLogger("vts.delivery")


async def process_one_delivery(session_factory, settings, attempt_id) -> None:
    async with session_factory() as session:
        repo = Repo(session)
        attempt = await session.get(DeliveryAttempt, attempt_id)
        if attempt is None:
            return
        now = utcnow()
        try:
            # Resolve the adapter FIRST: if the plugin is not loaded there is no
            # point reading artifacts or decrypting secrets for a delivery nobody
            # can execute. Raises UnknownAdapter, handled below as transient.
            adapter = get_adapter(attempt.adapter)

            task = await repo.get_task_by_id(attempt.task_id)
            if task is None:
                raise RuntimeError(f"task {attempt.task_id} is gone")

            payload = resolve_variant(task, attempt.variant)
            target = (
                await repo.get_delivery_target(task.user_id, attempt.target_id)
                if attempt.target_id
                else None
            )
            secrets: dict[str, str] = {}
            if target is not None and target.secrets_enc:
                secrets = decrypt_secrets(target.secrets_enc, load_secrets_key(settings))
            cfg = DeliveryTargetConfig(
                config=(target.config_json if target else {}) or {}, secrets=secrets
            )

            result = await adapter.deliver(payload, cfg)
            await repo.record_delivery_result(
                attempt.id,
                external_id=result.external_id,
                external_url=result.external_url,
            )
            await session.commit()
        except UnknownAdapter:
            # Transient, NOT a failure: the plugin is installed from an external
            # source and may simply not have loaded this restart. Park the row —
            # the attempt is refunded so a missing plugin can never exhaust
            # max_attempts and kill a delivery the user configured.
            await repo.park_delivery_for_adapter(
                attempt.id,
                next_attempt_at=now + timedelta(seconds=settings.delivery_adapter_wait_seconds),
            )
            await session.commit()
            logger.warning(
                "delivery %s parked: adapter %r not loaded; retrying in %ss",
                attempt.id,
                attempt.adapter,
                settings.delivery_adapter_wait_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — any other failure is retryable
            dead = attempt.attempts >= attempt.max_attempts
            next_at = (
                None
                if dead
                else now
                + timedelta(
                    seconds=backoff_seconds(
                        attempt.attempts,
                        settings.delivery_backoff_base_seconds,
                        settings.delivery_backoff_cap_seconds,
                    )
                )
            )
            await repo.record_delivery_failure(
                attempt.id,
                last_error=f"{type(exc).__name__}: {exc}",
                next_attempt_at=next_at,
                dead=dead,
            )
            await session.commit()
            logger.warning(
                "delivery %s failed (attempt %s/%s, dead=%s): %s",
                attempt.id,
                attempt.attempts,
                attempt.max_attempts,
                dead,
                exc,
            )


async def delivery_tick(session_factory, settings, now: datetime) -> int:
    """Reap stuck rows, claim a batch, process each. Returns how many ran."""
    async with session_factory() as session:
        repo = Repo(session)
        await repo.reap_stuck_deliveries(
            now - timedelta(seconds=settings.delivery_stuck_seconds)
        )
        claimed = await repo.claim_due_deliveries(now, limit=settings.delivery_claim_batch)
        await session.commit()
        ids = [row.id for row in claimed]

    for attempt_id in ids:
        await process_one_delivery(session_factory, settings, attempt_id)
    return len(ids)


async def delivery_loop(session_factory, settings, redis) -> None:
    """Background loop: tick on `delivery:notify`, else poll on a short timeout.

    Redis is only a wake-up. Correctness never depends on the message arriving —
    a pending row is picked up by the next timed tick regardless.
    """
    notify_channel = f"{settings.redis_prefix}delivery:notify"
    pubsub = redis.pubsub()
    await pubsub.subscribe(notify_channel)
    wakeup = asyncio.Event()

    async def _pump() -> None:
        async for _ in pubsub.listen():
            wakeup.set()

    pump = asyncio.create_task(_pump())
    try:
        while True:
            try:
                processed = await delivery_tick(session_factory, settings, utcnow())
            except Exception:
                logger.exception("delivery tick failed; continuing")
                processed = 0
            if not processed:
                wakeup.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=5.0)
            else:
                await asyncio.sleep(0.2)
    finally:
        pump.cancel()
        with suppress(BaseException):
            await pump
        with suppress(Exception):
            await pubsub.unsubscribe(notify_channel)
            await pubsub.aclose()
