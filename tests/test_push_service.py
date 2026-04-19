from __future__ import annotations

import asyncio
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pywebpush import WebPushException

from vts.core.config import Settings
from vts.services import push


def _settings_with_vapid() -> Settings:
    return Settings(
        vapid_public_key="pub",
        vapid_private_key="priv",
        vapid_subject="mailto:a@b.c",
    )


def _settings_without_vapid() -> Settings:
    return Settings(vapid_public_key=None, vapid_private_key=None)


def test_is_push_enabled() -> None:
    assert push.is_push_enabled(_settings_with_vapid()) is True
    assert push.is_push_enabled(_settings_without_vapid()) is False


def test_notify_user_skips_when_disabled() -> None:
    session = MagicMock()
    # No DB calls should happen when push is disabled.
    asyncio.run(push.notify_user(session, _settings_without_vapid(), uuid.uuid4(), {"x": 1}))
    session.execute.assert_not_called()


def _fake_sub(endpoint: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        endpoint=endpoint, p256dh="k1", auth="k2"
    )


def test_notify_user_drops_stale_404() -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    async def run() -> None:
        with patch.object(push, "list_subscriptions", AsyncMock(return_value=[_fake_sub("e1"), _fake_sub("e2")])):
            response = types.SimpleNamespace(status_code=410)
            exc = WebPushException("gone", response=response)
            # First endpoint returns 410, second succeeds.
            send_mock = MagicMock(side_effect=[exc, 201])
            with patch.object(push, "_send_sync", send_mock):
                await push.notify_user(
                    session,
                    _settings_with_vapid(),
                    uuid.uuid4(),
                    {"task_id": "abc", "status": "completed"},
                )

    asyncio.run(run())
    # We executed a DELETE for the stale endpoint and committed it.
    assert session.execute.await_count == 1
    assert session.commit.await_count == 1


def test_notify_user_keeps_sub_on_transient_error() -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    async def run() -> None:
        with patch.object(push, "list_subscriptions", AsyncMock(return_value=[_fake_sub("e1")])):
            response = types.SimpleNamespace(status_code=500)
            exc = WebPushException("server", response=response)
            with patch.object(push, "_send_sync", MagicMock(side_effect=exc)):
                await push.notify_user(
                    session,
                    _settings_with_vapid(),
                    uuid.uuid4(),
                    {"task_id": "abc", "status": "failed"},
                )

    asyncio.run(run())
    # No DELETE for transient failure.
    session.execute.assert_not_called()
