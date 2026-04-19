"""Web Push (VAPID) notification service."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vts.core.config import Settings
from vts.db.models import PushSubscription

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriptionPayload:
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str | None = None


def is_push_enabled(settings: Settings) -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


async def upsert_subscription(
    session: AsyncSession,
    user_id: uuid.UUID,
    payload: SubscriptionPayload,
) -> None:
    stmt = (
        pg_insert(PushSubscription)
        .values(
            user_id=user_id,
            endpoint=payload.endpoint,
            p256dh=payload.p256dh,
            auth=payload.auth,
            user_agent=payload.user_agent,
        )
        .on_conflict_do_update(
            index_elements=[PushSubscription.endpoint],
            set_={
                "user_id": user_id,
                "p256dh": payload.p256dh,
                "auth": payload.auth,
                "user_agent": payload.user_agent,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


async def delete_subscription(session: AsyncSession, endpoint: str) -> None:
    await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))
    await session.commit()


async def list_subscriptions(session: AsyncSession, user_id: uuid.UUID) -> list[PushSubscription]:
    result = await session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    return list(result.scalars().all())


def _send_sync(
    subscription_info: dict[str, Any],
    data: str,
    vapid_private_key: str,
    vapid_subject: str,
) -> int:
    # Returns the HTTP status code from the push service; raises WebPushException
    # on 4xx/5xx with response attached.
    response = webpush(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=vapid_private_key,
        vapid_claims={"sub": vapid_subject},
    )
    return getattr(response, "status_code", 201)


async def notify_user(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    """Send a push payload to every subscription for `user_id`.

    Subscriptions that the push service rejects as gone (404/410) are deleted
    so they don't pile up forever.
    """
    if not is_push_enabled(settings):
        return
    subs = await list_subscriptions(session, user_id)
    if not subs:
        return

    body = json.dumps(payload)
    stale_endpoints: list[str] = []

    for sub in subs:
        info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            await asyncio.to_thread(
                _send_sync,
                info,
                body,
                settings.vapid_private_key,  # type: ignore[arg-type]
                settings.vapid_subject,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None) if exc.response is not None else None
            if status in (404, 410):
                stale_endpoints.append(sub.endpoint)
                logger.info("push: dropping stale subscription %s (status=%s)", sub.endpoint, status)
            else:
                logger.warning("push: send failed (status=%s): %s", status, exc)
        except Exception as exc:  # pragma: no cover - network/unknown
            logger.warning("push: unexpected send error: %s", exc)

    if stale_endpoints:
        await session.execute(
            delete(PushSubscription).where(PushSubscription.endpoint.in_(stale_endpoints))
        )
        await session.commit()
