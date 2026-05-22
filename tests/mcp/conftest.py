from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vts.db.models import TaskStatus


@dataclass
class FakeTask:
    id: uuid.UUID
    user_id: uuid.UUID
    source_url: str
    source_title: str | None = None
    status: TaskStatus = TaskStatus.queued
    artifact_dir: str = "/tmp/vts-test/task"
    transcript_path: str | None = None
    summary_path: str | None = None
    error_message: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    summary_progress: dict[str, int] | None = None
    steps: list[Any] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class FakeRepo:
    """Mirrors the subset of vts.db.repo.Repo that the MCP tools call."""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, FakeTask] = {}
        self._asr_progress: dict[uuid.UUID, tuple[int, int]] = {}

    async def create_task(
        self,
        user_id: uuid.UUID,
        source_url: str,
        options: dict[str, Any],
        artifact_dir: str,
        task_id: uuid.UUID | None = None,
    ) -> FakeTask:
        task = FakeTask(
            id=task_id or uuid.uuid4(),
            user_id=user_id,
            source_url=source_url,
            artifact_dir=artifact_dir,
            options=options or {},
        )
        self.tasks[task.id] = task
        return task

    async def get_task_for_user(self, user_id: uuid.UUID, task_id: uuid.UUID) -> FakeTask | None:
        t = self.tasks.get(task_id)
        if t is None or t.user_id != user_id:
            return None
        return t

    async def get_asr_progress_for_tasks(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        return {tid: self._asr_progress.get(tid, (0, 0)) for tid in task_ids}

    async def list_tasks_for_user_filtered(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> list[FakeTask]:
        items = [t for t in self.tasks.values() if t.user_id == user_id]
        if status:
            items = [t for t in items if t.status == status]
        key_map = {
            "created_at": lambda t: t.created_at,
            "updated_at": lambda t: t.updated_at,
            "title": lambda t: (t.source_title or ""),
        }
        items.sort(key=key_map[sort], reverse=(order == "desc"))
        return items[:limit]


class FakeBus:
    """Mirrors the subset of vts.services.redis_bus.RedisBus that the MCP tools call."""

    def __init__(self) -> None:
        self.queued_notifications = 0
        self.published: list[dict[str, Any]] = []

    async def notify_queued(self) -> None:
        self.queued_notifications += 1

    async def publish_event(
        self,
        *,
        user_id: str,
        task_id: str,
        event: str,
        data: dict[str, Any],
        throttle_key: str | None = None,
    ) -> None:
        self.published.append(
            {"user_id": user_id, "task_id": task_id, "event": event, "data": data}
        )


@dataclass
class FakeUser:
    id: str
    username: str = "alice"


class _FakePubSub:
    def __init__(self, redis: "FakeRedisWithPubSub") -> None:
        self._redis = redis
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._redis._subscribers.setdefault(channel, []).append(self)

    async def unsubscribe(self, channel: str | None = None) -> None:
        chans = list(self._channels) if channel is None else [channel]
        for ch in chans:
            subs = self._redis._subscribers.get(ch, [])
            if self in subs:
                subs.remove(self)
            self._channels.discard(ch)

    async def close(self) -> None:
        await self.unsubscribe()

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None):
        try:
            payload = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "data": json.dumps(payload).encode("utf-8")}


class FakeRedisWithPubSub:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[_FakePubSub]] = {}

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        for sub in self._subscribers.get(channel, []):
            sub._queue.put_nowait(payload)
