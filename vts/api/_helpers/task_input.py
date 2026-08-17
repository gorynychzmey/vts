"""Validating task-creation input and handing a new task to the worker.

Shared by both creation paths — `POST /api/tasks/upload` and the resumable
`/api/uploads/*` flow — plus the queue-position cache the task list reads.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.orm.attributes import set_committed_value

from vts.api._helpers.serialization import serialize_task
from vts.api.schemas import TaskOut
from vts.db.repo import Repo
from vts.services.redis_bus import RedisBus
from vts.services.task_progress import summary_progress_for_task

logger = logging.getLogger(__name__)

_MAX_DISPLAY_NAME_CHARS = 500  # matches Text column; keep titles sane

def normalize_display_name(raw: str | None) -> str | None:
    """Normalize a user-supplied task title. Empty/whitespace-only input
    becomes None (so the UI falls back to source_url); otherwise trim
    surrounding whitespace and cap length to keep titles bounded."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_DISPLAY_NAME_CHARS]

_ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset(
    {
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
    }
)

def _normalize_delivery_json(delivery: str | None) -> list[dict]:
    """Parse the `delivery` form field of an upload into entry dicts.

    Uploads carry their options as form fields / JSON sidecars rather than a
    request model, so `delivery` arrives as a JSON string and needs the same
    shape check the URL path gets from DeliveryRef. Ownership and adapter
    availability are validated later, by validate_delivery_refs, exactly as on
    the URL path.
    """
    if not delivery:
        return []
    try:
        raw = json.loads(delivery)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="delivery must be valid JSON") from exc
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="delivery must be a JSON list")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail="each delivery entry must be an object")
        ref = entry.get("deliver_to")
        if not ref:
            raise HTTPException(status_code=422, detail="delivery entry requires 'deliver_to'")
        item: dict = {"deliver_to": str(ref)}
        variant = entry.get("variant")
        if variant:
            if variant not in ("raw", "redacted", "summary"):
                # May also be a prompt ref like "user:<uuid>" (vts-as1i).
                from vts.services.prompt_registry import parse_ref

                try:
                    parse_ref(str(variant))
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"invalid delivery variant: {variant!r}"
                    ) from exc
            item["variant"] = str(variant)
        out.append(item)
    return out

def _normalize_prompts_json(prompts: str | None) -> list[dict]:
    from vts.services.prompt_registry import parse_ref, ref_to_dict
    if prompts is None:
        return [{"source": "system", "id": "summary"}]
    try:
        raw_refs = json.loads(prompts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="prompts must be valid JSON") from exc
    if not isinstance(raw_refs, list):
        raise HTTPException(status_code=422, detail="prompts must be a JSON list")
    out: list[dict] = []
    for entry in raw_refs:
        try:
            source, ref_id = parse_ref(entry)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid prompt ref: {entry!r}") from exc
        out.append(ref_to_dict(source, ref_id))
    return out

async def _enqueue_uploaded_task(task, repo, redis, settings) -> "TaskOut":
    bus = RedisBus(redis, settings)
    await bus.notify_queued()
    await bus.publish_event(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event="task_status",
        data={"status": task.status.value},
    )
    set_committed_value(task, "steps", [])
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
    asr_progress = await repo.get_asr_progress_for_tasks([task.id])
    summary_progress = {task.id: summary_progress_for_task(task)}
    return serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)

_QUEUE_POS_CACHE_SUFFIX = "cache:queue_positions"

_QUEUE_POS_TTL_SECONDS = 2

async def _get_cached_queue_positions(
    redis: Redis, repo: Repo, prefix: str
) -> dict[uuid.UUID, int]:
    cache_key = f"{prefix}{_QUEUE_POS_CACHE_SUFFIX}"
    cached = await redis.get(cache_key)
    if cached is not None:
        raw: dict[str, int] = json.loads(cached)
        return {uuid.UUID(k): v for k, v in raw.items()}
    positions = await repo.get_global_queue_positions()
    serializable = {str(k): v for k, v in positions.items()}
    await redis.setex(cache_key, _QUEUE_POS_TTL_SECONDS, json.dumps(serializable))
    return positions

async def _get_lane_positions(redis: Redis, prefix: str) -> dict[uuid.UUID, tuple[str, int]]:
    raw = await redis.get(f"{prefix}queue:lanes")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[uuid.UUID, tuple[str, int]] = {}
    # network and ffmpeg map to their own distinct public queue names, each
    # with an independent counter. gpu_asr and gpu_llm both map to the
    # public "gpu" queue and share ONE counter — asr is numbered first since
    # it has scheduling priority in LaneManager, so an asr-waiting task
    # always gets a lower position than an llm-waiting task.
    groups: list[tuple[str, list[str]]] = [
        ("network", ["network"]),
        ("ffmpeg", ["ffmpeg"]),
        ("gpu", ["gpu_asr", "gpu_llm"]),
        ("diarize", ["diarize"]),
    ]
    for public, keys in groups:
        position = 0
        for key in keys:
            entries = data.get(key, [])
            if not isinstance(entries, list):
                continue
            for raw_id in entries:
                try:
                    tid = uuid.UUID(raw_id)
                except (ValueError, TypeError, AttributeError):
                    continue
                if tid in out:
                    continue
                position += 1
                out[tid] = (public, position)
    return out
