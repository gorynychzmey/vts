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


@dataclass
class FakePrompt:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    system_prompt: str
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class FakePreset:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    options: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class FakeDeliveryTarget:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    adapter: str
    config_json: dict[str, Any]
    secrets_enc: bytes | None = None
    # Set on targets (points at a credential); unused on credentials themselves.
    credential_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class FakeRepo:
    """Mirrors the subset of vts.db.repo.Repo that the MCP tools call."""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, FakeTask] = {}
        self._asr_progress: dict[uuid.UUID, tuple[int, int]] = {}
        self.prompts: dict[uuid.UUID, FakePrompt] = {}
        self.presets: dict[uuid.UUID, FakePreset] = {}
        self.default_presets: dict[uuid.UUID, dict | None] = {}
        self.last_options: dict[str, Any] | None = None
        self.delivery_targets: dict[uuid.UUID, Any] = {}
        self.delivery_credentials: dict[uuid.UUID, Any] = {}
        # Voice identification: task -> [names], and the registry behind it.
        # Kept as plain data so a test can set up "who appears where" without
        # constructing MatchDecision rows.
        self.task_people: dict[uuid.UUID, list[str]] = {}
        self.speakers: dict[uuid.UUID, Any] = {}

    async def create_task(
        self,
        user_id: uuid.UUID,
        source_url: str,
        options: dict[str, Any],
        artifact_dir: str,
        task_id: uuid.UUID | None = None,
    ) -> FakeTask:
        self.last_options = options or {}
        task = FakeTask(
            id=task_id or uuid.uuid4(),
            user_id=user_id,
            source_url=source_url,
            artifact_dir=artifact_dir,
            options=options or {},
        )
        self.tasks[task.id] = task
        return task

    # ---- DeliveryTarget CRUD (mirrors vts.db.repo.Repo delivery methods) ----

    async def create_delivery_credential(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        config: dict, secrets_enc: bytes | None,
    ) -> "FakeDeliveryTarget":
        credential = FakeDeliveryTarget(
            id=uuid.uuid4(), user_id=user_id, name=name, adapter=adapter,
            config_json=config, secrets_enc=secrets_enc,
        )
        self.delivery_credentials[credential.id] = credential
        return credential

    async def list_delivery_credentials(self, user_id: uuid.UUID) -> list["FakeDeliveryTarget"]:
        return [c for c in self.delivery_credentials.values() if c.user_id == user_id]

    async def get_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> "FakeDeliveryTarget | None":
        c = self.delivery_credentials.get(credential_id)
        return c if c is not None and c.user_id == user_id else None

    async def count_targets_for_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> int:
        return sum(
            1 for t in self.delivery_targets.values()
            if t.user_id == user_id and getattr(t, "credential_id", None) == credential_id
        )

    async def update_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        secrets_enc: bytes | None, clear_secrets: bool,
    ) -> "FakeDeliveryTarget | None":
        c = await self.get_delivery_credential(user_id, credential_id)
        if c is None:
            return None
        if name is not None:
            c.name = name
        if config is not None:
            c.config_json = config
        if clear_secrets:
            c.secrets_enc = None
        elif secrets_enc is not None:
            c.secrets_enc = secrets_enc
        return c

    async def delete_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> bool:
        c = await self.get_delivery_credential(user_id, credential_id)
        if c is None:
            return False
        del self.delivery_credentials[credential_id]
        return True

    async def create_delivery_target(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        credential_id: uuid.UUID, config: dict,
    ) -> "FakeDeliveryTarget":
        target = FakeDeliveryTarget(
            id=uuid.uuid4(), user_id=user_id, name=name, adapter=adapter,
            config_json=config, credential_id=credential_id,
        )
        self.delivery_targets[target.id] = target
        return target

    async def list_delivery_targets(self, user_id: uuid.UUID) -> list["FakeDeliveryTarget"]:
        return [t for t in self.delivery_targets.values() if t.user_id == user_id]

    async def get_delivery_target(
        self, user_id: uuid.UUID, target_id: uuid.UUID
    ) -> "FakeDeliveryTarget | None":
        t = self.delivery_targets.get(target_id)
        return t if t is not None and t.user_id == user_id else None

    async def get_delivery_target_by_name(
        self, user_id: uuid.UUID, name: str
    ) -> "FakeDeliveryTarget | None":
        for t in self.delivery_targets.values():
            if t.user_id == user_id and t.name == name:
                return t
        return None

    async def update_delivery_target(
        self, user_id: uuid.UUID, target_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        credential_id: uuid.UUID | None = None,
    ) -> "FakeDeliveryTarget | None":
        t = await self.get_delivery_target(user_id, target_id)
        if t is None:
            return None
        if name is not None:
            t.name = name
        if config is not None:
            t.config_json = config
        if credential_id is not None:
            t.credential_id = credential_id
        return t

    async def delete_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> bool:
        t = await self.get_delivery_target(user_id, target_id)
        if t is None:
            return False
        del self.delivery_targets[target_id]
        return True

    # ---- Prompt CRUD (mirrors vts.db.repo.Repo prompt methods) ----

    async def create_prompt(self, user_id: uuid.UUID, name: str, system_prompt: str) -> "FakePrompt":
        prompt = FakePrompt(
            id=uuid.uuid4(), user_id=user_id, name=name, system_prompt=system_prompt
        )
        self.prompts[prompt.id] = prompt
        return prompt

    async def list_prompts(self, user_id: uuid.UUID) -> list["FakePrompt"]:
        return [p for p in self.prompts.values() if p.user_id == user_id]

    async def get_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> "FakePrompt | None":
        p = self.prompts.get(prompt_id)
        if p is None or p.user_id != user_id:
            return None
        return p

    async def update_prompt(
        self,
        user_id: uuid.UUID,
        prompt_id: uuid.UUID,
        *,
        name: str | None,
        system_prompt: str | None,
    ) -> "FakePrompt | None":
        p = await self.get_prompt(user_id, prompt_id)
        if p is None:
            return None
        if name is not None:
            p.name = name
        if system_prompt is not None:
            p.system_prompt = system_prompt
        return p

    async def delete_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> bool:
        p = await self.get_prompt(user_id, prompt_id)
        if p is None:
            return False
        del self.prompts[prompt_id]
        return True

    # ---- Preset CRUD + default (mirrors vts.db.repo.Repo preset methods) ----

    async def create_preset(self, user_id: uuid.UUID, name: str, options: dict) -> "FakePreset":
        preset = FakePreset(id=uuid.uuid4(), user_id=user_id, name=name, options=dict(options))
        self.presets[preset.id] = preset
        return preset

    async def list_presets(self, user_id: uuid.UUID) -> list["FakePreset"]:
        return [p for p in self.presets.values() if p.user_id == user_id]

    async def get_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> "FakePreset | None":
        p = self.presets.get(preset_id)
        if p is None or p.user_id != user_id:
            return None
        return p

    async def update_preset(
        self,
        user_id: uuid.UUID,
        preset_id: uuid.UUID,
        *,
        name: str | None,
        options: dict | None,
    ) -> "FakePreset | None":
        p = await self.get_preset(user_id, preset_id)
        if p is None:
            return None
        if name is not None:
            p.name = name
        if options is not None:
            p.options = dict(options)
        return p

    async def delete_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> bool:
        p = await self.get_preset(user_id, preset_id)
        if p is None:
            return False
        del self.presets[preset_id]
        if self.default_presets.get(user_id) == {"source": "user", "id": str(preset_id)}:
            self.default_presets[user_id] = None
        return True

    async def get_user_default_preset(self, user_id: uuid.UUID) -> dict | None:
        return self.default_presets.get(user_id)

    async def set_user_default_preset(self, user_id: uuid.UUID, ref: dict | None) -> None:
        self.default_presets[user_id] = ref

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

    async def speaker_names_for_tasks(
        self, user_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        return {
            tid: list(self.task_people.get(tid, []))
            for tid in task_ids
            if self.task_people.get(tid)
        }

    async def tasks_featuring_speaker(
        self, user_id: uuid.UUID, speaker_id: uuid.UUID
    ) -> list[uuid.UUID]:
        person = self.speakers.get(speaker_id)
        if person is None:
            return []
        return [
            tid for tid, names in self.task_people.items()
            if person.name in names
        ]

    async def speakers_by_name(self, user_id: uuid.UUID, name: str) -> list[Any]:
        needle = (name or "").strip().lower()
        if not needle:
            return []
        return [
            p for p in self.speakers.values()
            if p.user_id == user_id and needle in p.name.lower()
        ]

    async def diarized_task_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        return [tid for tid, names in self.task_people.items() if names]

    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple | None = None,
        after: tuple | None = None,
        order: str = "desc",
        limit: int = 20,
        status: Any = None,
        q: str | None = None,
        created_from: Any = None,
        created_to: Any = None,
        source_type: str | None = None,
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
    ) -> list[FakeTask]:
        items = [t for t in self.tasks.values() if t.user_id == user_id]
        if task_ids is not None:
            allowed = {str(i) for i in task_ids}
            items = [t for t in items if str(t.id) in allowed]
        if exclude_task_ids:
            denied = {str(i) for i in exclude_task_ids}
            items = [t for t in items if str(t.id) not in denied]
        if status is not None:
            # Real repo receives a TaskStatus enum; FakeTask.status is a str.
            want = getattr(status, "value", status)
            items = [t for t in items if t.status == want]
        # Mirror the real filters (vts-rhx) rather than ignoring them, so a
        # test asserting a filtered MCP page actually exercises the filter.
        if q:
            needle = q.lower()
            items = [
                t for t in items
                if needle in (t.source_title or "").lower()
                or needle in (t.source_url or "").lower()
            ]
        if created_from is not None:
            items = [t for t in items if t.created_at >= created_from]
        if created_to is not None:
            items = [t for t in items if t.created_at <= created_to]
        if source_type == "file":
            items = [t for t in items if (t.source_url or "").startswith("file://")]
        elif source_type == "url":
            items = [t for t in items if not (t.source_url or "").startswith("file://")]
        items.sort(key=lambda t: (t.created_at, str(t.id)), reverse=(order == "desc"))
        if before is not None:
            b_ts, b_id = before
            items = [t for t in items if (t.created_at, str(t.id)) < (b_ts, str(b_id))]
        if after is not None:
            a_ts, a_id = after
            items = [t for t in items if (t.created_at, str(t.id)) > (a_ts, str(a_id))]
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
