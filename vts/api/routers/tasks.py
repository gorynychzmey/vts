"""Task lifecycle: create, list, inspect, mutate, and the live event stream.

The core of the API — creating tasks from a URL or an uploaded file, paging
the task list, and the batch transitions (pause / resume / delete / archive /
restart summary). `/api/events` lives here too: it is the SSE channel over
task state.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged.

This is the most connected router: it leans on 19 shared helpers
(`serialize_task`, the summary-reset family, the queue-position cache, ...).
They live in `vts.api._helpers` rather than here because other routers need
them too — four of them render tasks.

No `tags=` on the router: `_install_custom_openapi()` in `vts.api.main`
derives the OpenAPI tag from the URL prefix, and an explicit tag overrides it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from vts import __version__
from vts.api._helpers.artifact_store import _archive_task_artifacts, _rebuild_finalize_tail, _reset_final_summary_artifacts, _reset_final_summary_step
from vts.api._helpers.serialization import can_pause_task, can_restart_final_summary_task, can_restart_summary_task, can_resume_task, serialize_task, serialize_task_compact
from vts.api._helpers.task_input import _ALLOWED_UPLOAD_SUFFIXES, _enqueue_uploaded_task, _get_cached_queue_positions, _get_lane_positions, _normalize_delivery_json, _normalize_prompts_json, normalize_display_name
from vts.api.deps import (
    get_current_user,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    BatchResultOut,
    MessageOut,
    RestartSummaryRequest,
    TaskCompactOut,
    TaskCreateRequest,
    TaskIdsRequest,
    TaskOut,
    TaskUpdate,
)
from vts.core.config import Settings
from vts.db.models import Task, TaskStatus
from vts.db.repo import Repo
from vts.services import task_status as _ts
from vts.services.auth import AuthenticatedUser
from vts.services.redis_bus import RedisBus
from vts.services.storage import task_dir
from vts.services.summary_restart import WORKER_HELD_STATUSES as _WORKER_HELD_STATUSES
from vts.services.summary_restart import reset_task_for_summary_restart
from vts.services.task_progress import summary_progress_for_task

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/tasks", response_model=TaskOut)
async def create_task(
    request: TaskCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TaskOut:
    from vts.services.delivery_submit import (
        DeliveryValidationError,
        validate_delivery_refs,
    )

    repo = Repo(session)
    effective_user_id = uuid.UUID(user.id)
    options = request.model_dump()
    options.pop("url", None)
    # Explicit submit: an unknown target or an unavailable adapter is an
    # error the caller should see now, before any work is done.
    try:
        options["delivery"] = await validate_delivery_refs(
            repo, effective_user_id, options.get("delivery")
        )
    except DeliveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task_id = uuid.uuid4()
    artifact = task_dir(settings.artifacts_root, user.username, task_id)
    artifact.mkdir(parents=True, exist_ok=True)
    task = await repo.create_task(
        user_id=effective_user_id,
        source_url=request.url,
        options=options,
        artifact_dir=str(artifact),
        task_id=task_id,
    )
    await session.commit()
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


@router.get("/api/tasks/{task_id}/results/{source}/{ref}", include_in_schema=False)
async def get_prompt_result(
    task_id: uuid.UUID,
    source: str,
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PlainTextResponse:
    repo = Repo(session)
    task = await repo.get_task_by_id(task_id)
    if task is None or str(task.user_id) != user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    from vts.services.prompt_results import resolve_result_path
    path = resolve_result_path(task, source, ref)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Result not found")
    return PlainTextResponse(Path(path).read_text(encoding="utf-8"))


@router.post("/api/tasks/upload", response_model=TaskOut)
async def upload_task(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    # Accepted and ignored: existing clients (stale tabs, scripts) still post
    # it. Rejecting would break them for a flag that never did anything here.
    audio_only: bool = Form(default=False),  # noqa: ARG001
    transcript: bool = Form(default=True),
    diarize: bool = Form(default=False),
    prompts: str | None = Form(default=None),
    delivery: str | None = Form(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TaskOut:
    normalized_prompts = _normalize_prompts_json(prompts)
    normalized_delivery = _normalize_delivery_json(delivery)
    if normalized_prompts and not transcript:
        raise HTTPException(status_code=422, detail="prompts require transcript")
    if diarize and not transcript:
        raise HTTPException(status_code=422, detail="diarize requires transcript")
    original_filename = file.filename or "upload"
    suffix = Path(original_filename).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix or '(none)'}")

    from vts.services.delivery_submit import (
        DeliveryValidationError,
        validate_delivery_refs,
    )

    repo = Repo(session)
    effective_user_id = uuid.UUID(user.id)
    # Same rule as the URL path: an explicit submit naming an unknown
    # target or an unavailable adapter fails now, before the bytes are
    # written, rather than at delivery time.
    try:
        normalized_delivery = await validate_delivery_refs(
            repo, effective_user_id, normalized_delivery
        )
    except DeliveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task_id = uuid.uuid4()
    artifact = task_dir(settings.artifacts_root, user.username, task_id)
    artifact.mkdir(parents=True, exist_ok=True)
    media_dir = artifact / "media"
    media_dir.mkdir(exist_ok=True)

    safe_name = "audio.original" + suffix
    dest = media_dir / safe_name
    content = await file.read()
    await asyncio.to_thread(dest.write_bytes, content)

    source_url = f"file://{Path(original_filename).name}"
    options = {
        "language": language or None,
        # See /api/uploads/init: file:// tasks are never downloaded, so the
        # yt-dlp audio_only hint is normalized away rather than trusted.
        "audio_only": False,
        "transcript": transcript,
        # Persist explicitly, even when false: diarize_enabled() treats a
        # missing key as "unset" and falls back to the server default, so
        # dropping it here silently overrode the user's choice (vts-552).
        "diarize": diarize,
        "prompts": normalized_prompts,
        "delivery": normalized_delivery,
    }
    task = await repo.create_task(
        user_id=effective_user_id,
        source_url=source_url,
        options=options,
        artifact_dir=str(artifact),
        task_id=task_id,
        source_title=normalize_display_name(display_name),
    )
    await session.commit()
    return await _enqueue_uploaded_task(task, repo, redis, settings)


@router.get(
    "/api/tasks",
    response_model=list[TaskOut] | list[TaskCompactOut],
)
async def list_tasks(
    limit: int | None = None,
    offset: int = 0,
    compact: bool = False,
    before_ts: datetime | None = None,
    before_id: uuid.UUID | None = None,
    after_ts: datetime | None = None,
    after_id: uuid.UUID | None = None,
    order: str = "desc",
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    source_type: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> list[TaskOut] | list[TaskCompactOut]:
    """List tasks owned by the current user, newest first. Paginate with
    `limit`/`offset` or the cursors `before_ts`/`before_id` and
    `after_ts`/`after_id` plus `order`; `compact=true` for slim records.
    Filter with `q` (title or URL), `created_from`/`created_to`, and
    `source_type` (`file` or `url`)."""
    if limit is not None and limit < 0:
        raise HTTPException(status_code=422, detail="limit must be non-negative")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be non-negative")
    if limit is not None and limit > 500:
        raise HTTPException(status_code=422, detail="limit must be <= 500")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="order must be 'asc' or 'desc'")
    if source_type is not None and source_type not in ("file", "url"):
        raise HTTPException(
            status_code=422, detail="source_type must be 'file' or 'url'"
        )
    if created_from is not None and created_to is not None and created_from > created_to:
        # Rejected rather than returning an empty list: an inverted range
        # is a caller mistake, and silently returning nothing looks like
        # "you have no tasks".
        raise HTTPException(
            status_code=422, detail="created_from must not be after created_to"
        )
    if (before_ts is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_ts and before_id must be supplied together")
    if (after_ts is None) != (after_id is None):
        raise HTTPException(status_code=422, detail="after_ts and after_id must be supplied together")
    before = (before_ts, before_id) if before_ts is not None else None
    after = (after_ts, after_id) if after_ts is not None else None
    if before is not None and after is not None and not (after < before):
        raise HTTPException(status_code=422, detail="after cursor must be older than before cursor")
    repo = Repo(session)
    filters = {
        "q": q,
        "created_from": created_from,
        "created_to": created_to,
        "source_type": source_type,
    }
    filtering = any(v is not None and v != "" for v in filters.values())
    if before is not None or after is not None or filtering:
        # A filtered request goes through the cursor query even without a
        # cursor: it is the only one that knows the filters, and it
        # degrades to "newest first, limited" when no cursor is given —
        # which is exactly what the legacy branch did.
        tasks = await repo.list_tasks_page(
            uuid.UUID(user.id),
            before=before,
            after=after,
            order=order,
            limit=limit if limit is not None else settings.tasks_page_size,
            **filters,
        )
    else:
        tasks = await repo.list_tasks_for_user(
            uuid.UUID(user.id), limit=limit, offset=offset,
        )
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
    task_ids = [task.id for task in tasks]
    asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
    summary_progress = {task.id: summary_progress_for_task(task) for task in tasks}
    if compact:
        return [
            serialize_task_compact(task, queue_positions, asr_progress, summary_progress, lane_positions)
            for task in tasks
        ]
    return [
        serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)
        for task in tasks
    ]


@router.get("/api/tasks/queue-positions", include_in_schema=False)
async def get_queue_positions(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    repo = Repo(session)
    positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    return JSONResponse({str(k): v for k, v in positions.items()})


@router.get("/api/tasks/count", include_in_schema=False)
async def count_tasks(
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    source_type: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> JSONResponse:
    """How many tasks match the current filters, ignoring pagination.

    The list endpoint returns one PAGE, so the UI cannot derive this by counting
    what it rendered — with infinite scroll that would show "8" until you
    scrolled. Same filter arguments as `/api/tasks`, validated the same way, so
    a request the list rejects is not silently counted here instead.

    MUST stay above `/api/tasks/{task_id}`: that route would otherwise match
    "count" and fail parsing it as a UUID (the same ordering constraint the
    queue-positions route above has).
    """
    if source_type is not None and source_type not in ("file", "url"):
        raise HTTPException(
            status_code=422, detail="source_type must be 'file' or 'url'"
        )
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=422, detail="created_from must not be after created_to"
        )
    repo = Repo(session)
    total = await repo.count_tasks_page(
        uuid.UUID(user.id),
        q=q,
        created_from=created_from,
        created_to=created_to,
        source_type=source_type,
    )
    return JSONResponse({"total": total})


@router.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TaskOut:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
    asr_progress = await repo.get_asr_progress_for_tasks([task.id])
    summary_progress = {task.id: summary_progress_for_task(task)}
    return serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)


@router.patch("/api/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TaskOut:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task.source_title = normalize_display_name(payload.display_name)
    await session.commit()
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
    asr_progress = await repo.get_asr_progress_for_tasks([task.id])
    summary_progress = {task.id: summary_progress_for_task(task)}
    return serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)


@router.post("/api/tasks/{task_id}/restart_summary", response_model=MessageOut)
async def restart_summary_task(
    task_id: uuid.UUID,
    request: RestartSummaryRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> MessageOut:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    bus = RedisBus(redis, settings)
    from vts.services.prompt_results import downgrade_system_summary_entry

    artifact_resets: list[asyncio.Task[None]] = []
    if request.mode == "final_only":
        if not can_restart_final_summary_task(task):
            raise HTTPException(
                status_code=409,
                detail=f"cannot_restart_final:{task.status.value}",
            )
        if request.prompts is not None:
            # New-set restart: swap options.prompts, clear all finalize
            # results, rebuild the finalize tail, re-queue.
            from vts.services.prompt_results import clear_all_finalize_results

            new_refs = [
                {"source": p.source, "id": p.id} for p in request.prompts
            ]
            clear_all_finalize_results(task)  # files, prompt_results, summary_path
            new_options = dict(task.options or {})
            new_options["prompts"] = new_refs
            task.options = new_options
            await _rebuild_finalize_tail(repo, task, new_options)
        else:
            _reset_final_summary_step(task)
            downgrade_system_summary_entry(task)
            artifact_resets.append(asyncio.to_thread(_reset_final_summary_artifacts, task))
    else:
        if not can_restart_summary_task(task):
            raise HTTPException(
                status_code=409,
                detail=f"cannot_restart:{task.status.value}",
            )
        if task.status in _WORKER_HELD_STATUSES:
            # A worker still holds this task, so resetting the artefacts here
            # would race the step writing to them. Flag the restart, cancel
            # the task, and return: the worker resets once it has reaped it
            # (WorkerPool._restart_if_requested). Returning "restarting"
            # rather than "queued" keeps the response honest — the task is
            # not queued yet, and will not be for a second or two.
            #
            # Only these statuses defer. `paused` looks like it belongs here
            # and does not: the worker does NOT hold a paused task — it is
            # absent from WorkerPool._active, which is the only thing `reap`
            # iterates — so the deferred reset would never run and the task
            # would sit paused forever behind a "restarting" reply. With no
            # worker holding it there is no race to avoid either, so it takes
            # the synchronous path below like any other idle task (vts-gouq).
            await bus.request_restart(task.id)
            await bus.request_cancel(task.id)
            return MessageOut(status="restarting")
        # A paused task carries the pause flag, and a task canceled earlier may
        # still carry the cancel flag; either would make `admit` divert the
        # task we are about to queue. Clear both before queuing it.
        await bus.clear_pause_request(task.id)
        await bus.clear_cancel_request(task.id)
        await reset_task_for_summary_restart(repo, task)
        await bus.notify_queued()
        return MessageOut(status="queued")
    task.summary_path = None
    await repo.set_task_summary_progress(task, 0, 0)
    await repo.set_task_status(task, TaskStatus.queued)
    await asyncio.gather(*artifact_resets)
    await session.commit()
    await bus.notify_queued()
    return MessageOut(status="queued")


@router.post("/api/tasks/pause", response_model=BatchResultOut)
async def pause_tasks(
    request: TaskIdsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> BatchResultOut:
    repo = Repo(session)
    bus = RedisBus(redis, settings)
    tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
    task_map = {task.id: task for task in tasks}
    results: dict[str, str] = {}
    for task_id in request.task_ids:
        tid = str(task_id)
        task = task_map.get(task_id)
        if task is None:
            results[tid] = "not_found"
            continue
        if not can_pause_task(task.status):
            results[tid] = f"cannot_pause:{task.status.value}"
            continue
        await repo.set_task_status(task, TaskStatus.paused)
        await bus.request_pause(task_id)
        results[tid] = "paused"
    await session.commit()
    return BatchResultOut(results=results)


@router.post("/api/tasks/resume", response_model=BatchResultOut)
async def resume_tasks(
    request: TaskIdsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> BatchResultOut:
    repo = Repo(session)
    tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
    task_map = {task.id: task for task in tasks}
    results: dict[str, str] = {}
    bus = RedisBus(redis, settings)
    for task_id in request.task_ids:
        tid = str(task_id)
        task = task_map.get(task_id)
        if task is None:
            results[tid] = "not_found"
            continue
        if not can_resume_task(task.status):
            results[tid] = f"cannot_resume:{task.status.value}"
            continue
        # Clearing the pause flag alone is not enough. `admit` checks the
        # cancel flag first and diverts the task straight to `canceled`
        # without ever starting it, logging only "skipping canceled task
        # before start" — so a resume of a task that carries a stale cancel
        # flag reads to the user as a silent delete. A stale restart flag is
        # just as bad the other way: reap would reset the artefacts of a task
        # the user asked to carry on with. Resume means "run this task as it
        # is", so it withdraws every pending request against it (vts-gouq).
        await bus.clear_pause_request(task_id)
        await bus.clear_cancel_request(task_id)
        await bus.clear_restart_request(task_id)
        await repo.set_task_status(task, TaskStatus.queued)
        results[tid] = "queued"
    await session.commit()
    if any(v == "queued" for v in results.values()):
        await bus.notify_queued()
    return BatchResultOut(results=results)


@router.delete("/api/tasks", response_model=BatchResultOut)
async def delete_tasks(
    request: TaskIdsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> BatchResultOut:
    repo = Repo(session)
    tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
    task_map = {task.id: task for task in tasks}
    results: dict[str, str] = {}
    bus = RedisBus(redis, settings)
    artifacts_to_remove: list[Path] = []
    tasks_to_delete: list = []
    for task_id in request.task_ids:
        tid = str(task_id)
        task = task_map.get(task_id)
        if task is None:
            results[tid] = "not_found"
            continue
        tasks_to_delete.append(task)
        results[tid] = "deleted"
    if tasks_to_delete:
        await asyncio.gather(
            *[bus.request_cancel(t.id) for t in tasks_to_delete],
        )
        for task in tasks_to_delete:
            await repo.set_task_status(task, TaskStatus.canceled)
            artifacts_to_remove.append(Path(task.artifact_dir))
            await session.delete(task)
    await session.commit()
    await asyncio.gather(
        *[asyncio.to_thread(shutil.rmtree, artifact, True) for artifact in artifacts_to_remove]
    )
    return BatchResultOut(results=results)


@router.post("/api/tasks/archive", response_model=BatchResultOut)
async def archive_tasks(
    request: TaskIdsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> BatchResultOut:
    repo = Repo(session)
    tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
    task_map = {task.id: task for task in tasks}
    results: dict[str, str] = {}
    for task_id in request.task_ids:
        tid = str(task_id)
        task = task_map.get(task_id)
        if task is None:
            results[tid] = "not_found"
            continue
        if not _ts.can_archive(task.status):
            results[tid] = f"cannot_archive:{task.status.value}"
            continue
        await asyncio.to_thread(_archive_task_artifacts, task)
        await repo.set_task_status(task, TaskStatus.archived)
        results[tid] = "archived"
    await session.commit()
    return BatchResultOut(results=results)


async def _client_gone(request: Request) -> bool:
    """Resolve once the connection is gone.

    `is_disconnected()` answers immediately, so it is polled rather than
    awaited. One second is a compromise: fast enough that a graceful stop
    is not held up noticeably, slow enough to stay cheap for an idle
    stream that may live for hours.
    """
    while True:
        if await request.is_disconnected():
            return True
        await asyncio.sleep(1.0)


@router.get("/api/events", include_in_schema=False)
async def get_events(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    async def event_generator() -> Any:
        yield f"event: server_version\ndata: {json.dumps({'version': __version__}, ensure_ascii=True)}\n\n"
        # Set by the lifespan on the way out. This stream would otherwise
        # outlive the shutdown and hold uvicorn open (vts-9er).
        # `request.app` rather than a closed-over `app`: this handler now lives
        # in a router, and the flag is still set by the lifespan on the way out
        # (vts-9er) — losing it would let this stream outlive shutdown again.
        shutting_down: asyncio.Event | None = getattr(
            request.app.state, "shutting_down", None
        )
        pubsub = redis.pubsub()
        channel = f"{settings.redis_prefix}events"
        await pubsub.subscribe(channel)
        try:
            while True:
                if shutting_down is not None and shutting_down.is_set():
                    # Second line of defence, and the one that gets a word
                    # in: the client reconnects at once instead of waiting
                    # out its own error backoff.
                    yield "event: server_shutdown\ndata: {}\n\n"
                    return

                read = asyncio.ensure_future(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
                )
                waiters: set[asyncio.Future] = {read}
                stop: asyncio.Future | None = None
                if shutting_down is not None:
                    stop = asyncio.ensure_future(shutting_down.wait())
                    waiters.add(stop)
                # Poll for disconnection alongside the read: without it the
                # loop would sit in the 30s pubsub wait and only notice the
                # closed connection afterwards, which is most of what a
                # graceful shutdown is waiting on.
                gone = asyncio.ensure_future(_client_gone(request))
                waiters.add(gone)
                # Race the read against the shutdown rather than polling on
                # a short timeout: the loop keeps waking on its original
                # ~30s cadence, but a shutdown is noticed immediately.
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                except BaseException:
                    # The client hung up while we were parked here, so the
                    # cancellation lands inside the wait. `read` is an
                    # independent task, not a child of this one: nothing
                    # cancels it on our way out, and it later fails with a
                    # redis ConnectionError that no one retrieves — recurring
                    # log noise plus a leaked future per disconnect
                    # (vts-9tr3). The two below are handled in `finally`
                    # because they are cancelled on every normal pass too.
                    read.cancel()
                    raise
                finally:
                    if stop is not None and not stop.done():
                        stop.cancel()
                    if not gone.done():
                        gone.cancel()

                if gone.done() and not gone.cancelled() and gone.result():
                    # Client (or uvicorn, on its behalf) hung up.
                    read.cancel()
                    with suppress(asyncio.CancelledError):
                        await read
                    return

                if not read.done():
                    # Shutdown won the race: drop the pending read and let
                    # the check at the top of the loop emit the farewell.
                    read.cancel()
                    with suppress(asyncio.CancelledError):
                        await read
                    continue

                message = read.result()
                if not message:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                data = json.loads(message["data"].decode("utf-8"))
                if data.get("user_id") != user.id:
                    continue
                yield f"event: {data.get('event', 'message')}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
