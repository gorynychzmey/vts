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
from vts.api.schemas import AdminUsersOut, BatchResultOut, MeOut, MessageOut, TaskCreateRequest, TaskIdsRequest, TaskOut
from vts.core.config import Settings
from vts.core.failures import classify_failure_code
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


SUMMARY_STEP_NAMES = frozenset(
    {
        "prepare_llama_model",
        "prepare_summary_chunks",
        "summarize_windows",
        "summarize_final",
    }
)


def can_restart_summary_task(task: Task) -> bool:
    options = task.options if isinstance(task.options, dict) else {}
    if options.get("summary") is False:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return any(step.name in SUMMARY_STEP_NAMES and step.status == StepStatus.failed for step in task.steps)


def can_restart_final_summary_task(task: Task) -> bool:
    options = task.options if isinstance(task.options, dict) else {}
    if options.get("summary") is False:
        return False
    summarize_windows_status = _find_step_status(task, "summarize_windows")
    if summarize_windows_status != StepStatus.completed:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return _find_step_status(task, "summarize_final") == StepStatus.failed


ARCHIVED_LOG_MESSAGE = "__VTS_LOG_ARCHIVED__"


def _is_path_within(root: Path, path: Path) -> bool:
    try:
        root_resolved = root.resolve()
        path_resolved = path.resolve()
    except OSError:
        return False
    return path_resolved == root_resolved or root_resolved in path_resolved.parents


def _archive_task_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return
    try:
        root_resolved = artifact_root.resolve()
    except OSError:
        return

    keep_files: set[Path] = set()
    for raw_path in (task.transcript_path, task.summary_path):
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        if _is_path_within(root_resolved, path):
            keep_files.add(path.resolve())

    log_path = artifact_root / "logs" / "task.log"
    try:
        log_resolved = log_path.resolve(strict=False)
    except OSError:
        log_resolved = log_path

    for file_path in artifact_root.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            file_resolved = file_path.resolve()
        except OSError:
            continue
        if file_resolved in keep_files or file_resolved == log_resolved:
            continue
        file_path.unlink(missing_ok=True)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"{ARCHIVED_LOG_MESSAGE}\n", encoding="utf-8")

    directories = sorted(
        (path for path in artifact_root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        if directory == artifact_root:
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
        except OSError:
            continue


def _reset_summary_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return

    summary_dir = artifact_root / "summary"
    outputs_dir = artifact_root / "outputs"

    for path in summary_dir.glob("window_*.txt"):
        path.unlink(missing_ok=True)

    for path in (
        summary_dir / "chunks.json",
        summary_dir / "windows.json",
        summary_dir / "final.json",
        summary_dir / "final.md",
        outputs_dir / "llama_model_ready.json",
        outputs_dir / "summary_chunks.json",
        outputs_dir / "window_summaries.json",
        outputs_dir / "summary.json",
        outputs_dir / "summary.md",
    ):
        path.unlink(missing_ok=True)


def _reset_summary_steps(task: Task) -> None:
    for step in task.steps:
        if step.name not in SUMMARY_STEP_NAMES:
            continue
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None


def _reset_final_summary_step(task: Task) -> None:
    for step in task.steps:
        if step.name != "summarize_final":
            continue
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None


def _reset_final_summary_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return
    summary_dir = artifact_root / "summary"
    outputs_dir = artifact_root / "outputs"
    for path in (
        summary_dir / "final.json",
        summary_dir / "final.md",
        outputs_dir / "summary.json",
        outputs_dir / "summary.md",
    ):
        path.unlink(missing_ok=True)


def _find_step_status(task: Task, step_name: str) -> StepStatus | None:
    for step in task.steps:
        if step.name == step_name:
            return step.status
    return None


def _summary_progress_for_task(task: Task) -> tuple[int, int]:
    options = task.options if isinstance(task.options, dict) else {}
    if options.get("summary") is False:
        return (0, 0)
    prog = task.summary_progress
    if not isinstance(prog, dict):
        return (0, 0)
    current = prog.get("current", 0)
    total = prog.get("total", 0)
    return (max(int(current), 0), max(int(total), 0))


def _processing_seconds_for_task(task: Task) -> int | None:
    started = [step.started_at for step in task.steps if step.started_at is not None]
    finished = [step.finished_at for step in task.steps if step.finished_at is not None]
    if not started or not finished:
        return None
    duration = (max(finished) - min(started)).total_seconds()
    if duration < 0:
        return 0
    return int(duration)


def _text_length_from_path(path_value: str | None, *, prefer_json_text_field: bool = False) -> int | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    text_value = raw_text
    if prefer_json_text_field and path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        extracted = payload.get("text")
        if not isinstance(extracted, str):
            return None
        text_value = extracted

    return len(text_value.strip())


def _task_stats_for_serialization(task: Task) -> dict[str, int | None]:
    return {
        "processing_seconds": _processing_seconds_for_task(task),
        "transcript_chars": _text_length_from_path(task.transcript_path, prefer_json_text_field=True),
        "summary_chars": _text_length_from_path(task.summary_path, prefer_json_text_field=False),
    }


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
    failure_code = classify_failure_code(task.error_message)
    return TaskOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        queue_position=queue_position,
        options=task.options,
        transcript_path=task.transcript_path,
        summary_path=task.summary_path,
        error_message=task.error_message,
        failure_code=failure_code,
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
        stats=_task_stats_for_serialization(task),
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
        task.steps = []
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: _summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.get("/api/tasks", response_model=list[TaskOut])
    async def list_tasks(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> list[TaskOut]:
        repo = Repo(session)
        tasks = await repo.list_tasks_for_user(uuid.UUID(user.id))
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        task_ids = [task.id for task in tasks]
        asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
        summary_progress = {task.id: _summary_progress_for_task(task) for task in tasks}
        return [serialize_task(task, queue_positions, asr_progress, summary_progress) for task in tasks]

    @app.get("/api/tasks/queue-positions")
    async def get_queue_positions(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> JSONResponse:
        repo = Repo(session)
        positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        return JSONResponse({str(k): v for k, v in positions.items()})

    @app.get("/api/tasks/{task_id}", response_model=TaskOut)
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

    @app.post("/api/tasks/{task_id}/restart_summary", response_model=MessageOut)
    async def restart_summary_task(
        task_id: uuid.UUID,
        mode: str = "full",
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> MessageOut:
        if mode not in ("full", "final_only"):
            raise HTTPException(status_code=422, detail="mode must be 'full' or 'final_only'")
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if mode == "final_only":
            if not can_restart_final_summary_task(task):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot restart final summary for task with status '{task.status.value}'",
                )
            _reset_final_summary_step(task)
            await asyncio.to_thread(_reset_final_summary_artifacts, task)
        else:
            if not can_restart_summary_task(task):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot restart summary for task with status '{task.status.value}'",
                )
            _reset_summary_steps(task)
            await asyncio.to_thread(_reset_summary_artifacts, task)
        task.summary_path = None
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
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> MessageOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        bus = RedisBus(redis, settings)
        await bus.request_cancel(task.id)
        await bus.remove_task_from_queue(task.id)
        await repo.set_task_status(task, TaskStatus.canceled)
        artifact = Path(task.artifact_dir)
        await session.delete(task)
        await session.commit()
        if artifact.exists():
            await asyncio.to_thread(shutil.rmtree, artifact, True)
        return MessageOut(status="deleted")

    @app.post("/api/tasks/{task_id}/archive", response_model=MessageOut)
    async def archive_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> MessageOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in {TaskStatus.completed, TaskStatus.failed}:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot archive task with status '{task.status.value}'",
            )
        await asyncio.to_thread(_archive_task_artifacts, task)
        await repo.set_task_status(task, TaskStatus.archived)
        await session.commit()
        return MessageOut(status="archived")

    @app.post("/api/tasks/pause", response_model=BatchResultOut)
    async def pause_tasks(
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
            if not can_pause_task(task.status):
                results[tid] = f"cannot_pause:{task.status.value}"
                continue
            await repo.set_task_status(task, TaskStatus.paused)
            results[tid] = "paused"
        await session.commit()
        return BatchResultOut(results=results)

    @app.post("/api/tasks/resume", response_model=BatchResultOut)
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
            await repo.set_task_status(task, TaskStatus.queued)
            results[tid] = "queued"
        await session.commit()
        for task_id in request.task_ids:
            if results.get(str(task_id)) == "queued":
                await bus.enqueue_task(task_id)
        return BatchResultOut(results=results)

    @app.delete("/api/tasks", response_model=BatchResultOut)
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
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            await bus.request_cancel(task.id)
            await bus.remove_task_from_queue(task.id)
            await repo.set_task_status(task, TaskStatus.canceled)
            artifacts_to_remove.append(Path(task.artifact_dir))
            await session.delete(task)
            results[tid] = "deleted"
        await session.commit()
        for artifact in artifacts_to_remove:
            if artifact.exists():
                await asyncio.to_thread(shutil.rmtree, artifact, True)
        return BatchResultOut(results=results)

    @app.post("/api/tasks/archive", response_model=BatchResultOut)
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
            if task.status not in {TaskStatus.completed, TaskStatus.failed}:
                results[tid] = f"cannot_archive:{task.status.value}"
                continue
            await asyncio.to_thread(_archive_task_artifacts, task)
            await repo.set_task_status(task, TaskStatus.archived)
            results[tid] = "archived"
        await session.commit()
        return BatchResultOut(results=results)

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
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
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
