from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vts import __version__
from vts.api.deps import get_current_user, get_redis, get_session_dep, get_settings_dep
from vts.api.schemas import AdminUsersOut, MeOut, MessageOut, TaskCreateRequest, TaskOut
from vts.core.config import Settings
from vts.core.logging import configure_logging
from vts.db.models import StepStatus, Task, TaskStatus
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.redis_bus import RedisBus
from vts.services.storage import task_dir


def can_pause_task(status: TaskStatus) -> bool:
    return status in {TaskStatus.queued, TaskStatus.running}


def can_resume_task(status: TaskStatus) -> bool:
    return status in {TaskStatus.paused, TaskStatus.failed}


def _read_json_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _list_count(payload: dict[str, Any] | None, key: str) -> int:
    if payload is None:
        return 0
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _find_step_status(task: Task, step_name: str) -> StepStatus | None:
    for step in task.steps:
        if step.name == step_name:
            return step.status
    return None


def _summary_progress_for_task(task: Task) -> tuple[int, int]:
    options = task.options if isinstance(task.options, dict) else {}
    if options.get("summary") is False:
        return (0, 0)

    artifact = Path(task.artifact_dir)
    summary_dir = artifact / "summary"
    outputs_dir = artifact / "outputs"

    chunks_payload = _read_json_payload(summary_dir / "chunks.json") or _read_json_payload(
        outputs_dir / "summary_chunks.json"
    )
    windows_payload = _read_json_payload(summary_dir / "windows.json") or _read_json_payload(
        outputs_dir / "window_summaries.json"
    )
    window_total = _list_count(chunks_payload, "chunks")
    window_done = _list_count(windows_payload, "windows")
    if window_total > 0:
        window_done = min(window_done, window_total)

    total_parts = window_total + 1 if window_total > 0 else 0
    final_step = _find_step_status(task, "summarize_final")
    if total_parts == 0 and final_step in {StepStatus.running, StepStatus.completed}:
        total_parts = 1

    current = window_done
    if final_step == StepStatus.running:
        current = max(current, window_total)
    elif final_step == StepStatus.completed:
        current = total_parts

    return (max(current, 0), max(total_parts, 0))


def serialize_task(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
) -> TaskOut:
    queue_position: int | None = None
    if queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    return TaskOut(
        id=task.id,
        source_url=task.source_url,
        status=task.status.value,
        queue_position=queue_position,
        options=task.options,
        transcript_path=task.transcript_path,
        summary_path=task.summary_path,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        steps=[
            {
                "name": step.name,
                "status": step.status.value,
                "attempt": step.attempt,
                "started_at": step.started_at,
                "finished_at": step.finished_at,
                "message": step.message,
            }
            for step in sorted(task.steps, key=lambda item: item.name)
        ],
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
    )


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings_dep()
    app = FastAPI(title="vts", version=__version__)
    static_dir = Path(__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    no_cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.on_event("startup")
    async def on_startup() -> None:
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await app.state.redis.aclose()

    @app.get("/", include_in_schema=False)
    async def root() -> HTMLResponse:
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        content = template.replace("__VTS_VERSION__", __version__)
        return HTMLResponse(content=content, headers=no_cache_headers)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/version")
    async def version() -> JSONResponse:
        return JSONResponse({"version": __version__}, headers=no_cache_headers)

    @app.get("/api/me", response_model=MeOut)
    async def me(user: AuthenticatedUser = Depends(get_current_user)) -> MeOut:
        return MeOut(requested_by=user.requested_by, acting_as=user.acting_as, is_admin=user.is_admin)

    @app.get("/api/admin/users", response_model=AdminUsersOut)
    async def admin_users(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> AdminUsersOut:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        repo = Repo(session)
        users = await repo.list_usernames()
        return AdminUsersOut(users=users)

    @app.post("/api/tasks", response_model=TaskOut)
    async def create_task(
        request: TaskCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        repo = Repo(session)
        effective_user_id = uuid.UUID(user.id)
        task_id = uuid.uuid4()
        artifact = task_dir(settings.artifacts_root, user.username, task_id)
        artifact.mkdir(parents=True, exist_ok=True)
        options = request.model_dump()
        options.pop("url", None)
        task = await repo.create_task(
            user_id=effective_user_id,
            source_url=request.url,
            options=options,
            artifact_dir=str(artifact),
            task_id=task_id,
        )
        await session.commit()
        bus = RedisBus(redis, settings)
        await bus.enqueue_task(task.id)
        await bus.publish_event(
            user_id=str(task.user_id),
            task_id=str(task.id),
            event="task_status",
            data={"status": task.status.value},
        )
        task = await repo.get_task_for_user(effective_user_id, task.id)
        if task is None:
            raise HTTPException(status_code=500, detail="Task not found after creation")
        queue_positions = await repo.get_global_queue_positions()
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: _summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.get("/api/tasks", response_model=list[TaskOut])
    async def list_tasks(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> list[TaskOut]:
        repo = Repo(session)
        tasks = await repo.list_tasks_for_user(uuid.UUID(user.id))
        queue_positions = await repo.get_global_queue_positions()
        task_ids = [task.id for task in tasks]
        asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
        summary_progress = {task.id: _summary_progress_for_task(task) for task in tasks}
        return [serialize_task(task, queue_positions, asr_progress, summary_progress) for task in tasks]

    @app.get("/api/tasks/{task_id}", response_model=TaskOut)
    async def get_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> TaskOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        queue_positions = await repo.get_global_queue_positions()
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: _summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.post("/api/tasks/{task_id}/pause", response_model=MessageOut)
    async def pause_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> MessageOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not can_pause_task(task.status):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot pause task with status '{task.status.value}'",
            )
        await repo.set_task_status(task, TaskStatus.paused)
        await session.commit()
        return MessageOut(status="paused")

    @app.post("/api/tasks/{task_id}/resume", response_model=MessageOut)
    async def resume_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> MessageOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not can_resume_task(task.status):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot resume task with status '{task.status.value}'",
            )
        await repo.set_task_status(task, TaskStatus.queued)
        await session.commit()
        bus = RedisBus(redis, settings)
        await bus.enqueue_task(task.id)
        return MessageOut(status="queued")

    @app.delete("/api/tasks/{task_id}", response_model=MessageOut)
    async def delete_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> MessageOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await repo.set_task_status(task, TaskStatus.canceled)
        artifact = Path(task.artifact_dir)
        await session.delete(task)
        await session.commit()
        if artifact.exists():
            await asyncio.to_thread(shutil.rmtree, artifact, True)
        return MessageOut(status="deleted")

    @app.get("/api/tasks/{task_id}/transcript")
    async def get_transcript(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.transcript_path:
            raise HTTPException(status_code=404, detail="Transcript is not ready")
        path = Path(task.transcript_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Transcript file missing")
        media_type = "text/plain; charset=utf-8" if path.suffix == ".txt" else "application/json"
        return Response(content=path.read_text(encoding="utf-8"), media_type=media_type)

    @app.get("/api/tasks/{task_id}/summary")
    async def get_summary(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.summary_path:
            raise HTTPException(status_code=404, detail="Summary is not ready")
        path = Path(task.summary_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Summary file missing")
        media_type = "text/markdown; charset=utf-8" if path.suffix in {".md", ".markdown"} else "application/json"
        return Response(content=path.read_text(encoding="utf-8"), media_type=media_type)

    @app.get("/api/tasks/{task_id}/log")
    async def get_log(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PlainTextResponse:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        path = Path(task.artifact_dir) / "logs" / "task.log"
        if not path.exists():
            return PlainTextResponse("", status_code=200)
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/events")
    async def get_events(
        user: AuthenticatedUser = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> StreamingResponse:
        async def event_generator() -> Any:
            pubsub = redis.pubsub()
            channel = f"{settings.redis_prefix}events"
            await pubsub.subscribe(channel)
            try:
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
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

    return app


app = create_app()
