from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.middleware.sessions import SessionMiddleware

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from vts import __version__
from vts.api.csrf import require_same_site
from vts.api.deps import (
    get_current_user,
    get_current_user_session_only,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    AdminUsersOut,
    ApiTokenCreateOut,
    ApiTokenCreateRequest,
    ApiTokenOut,
    BatchResultOut,
    MeOut,
    TaskCompactOut,
    PushConfigOut,
    PushStatusOut,
    PushSubscriptionIn,
    PushUnsubscribeIn,
    RestartSummaryRequest,
    TaskCreateRequest,
    TaskIdsRequest,
    TaskOut,
)
from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.core.logging import configure_logging
from vts.db.models import StepStatus, Task, TaskStatus
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.media_kind import media_content_type, media_kind
from vts.services.push import (
    SubscriptionPayload,
    delete_subscription,
    is_push_enabled,
    list_subscriptions,
    upsert_subscription,
)
from vts.services.redis_bus import RedisBus
from vts.services.storage import task_dir
from vts.services.task_progress import summary_progress_for_task


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
        outputs_dir / "redacted_transcript.txt",
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



def _processing_seconds_for_task(task: Task) -> int | None:
    started = [step.started_at for step in task.steps if step.started_at is not None]
    finished = [step.finished_at for step in task.steps if step.finished_at is not None]
    if not started or not finished:
        return None
    duration = (max(finished) - min(started)).total_seconds()
    if duration < 0:
        return 0
    return int(duration)


def _text_length_from_path(path_value: str | Path | None, *, prefer_json_text_field: bool = False) -> int | None:
    if not path_value:
        return None
    path = Path(path_value)
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
    redacted_path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
    return {
        "processing_seconds": _processing_seconds_for_task(task),
        "transcript_chars": _text_length_from_path(task.transcript_path, prefer_json_text_field=True),
        "summary_chars": _text_length_from_path(task.summary_path, prefer_json_text_field=False),
        "redacted_chars": _text_length_from_path(redacted_path, prefer_json_text_field=False),
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


def _find_media_file(artifact_dir: str | None) -> Path | None:
    if not artifact_dir:
        return None
    media_dir = Path(artifact_dir) / "media"
    for pattern in ("video.mkv", "audio.original.*"):
        matches = sorted(media_dir.glob(pattern)) if media_dir.exists() else []
        if matches:
            return matches[-1]
    return None


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
        redacted_path=str(Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt")
        if task.artifact_dir
        and (Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt").exists()
        else None,
        media_path=str(_mf) if (_mf := _find_media_file(task.artifact_dir)) else None,
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


def serialize_task_compact(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
) -> "TaskCompactOut":
    """Compact serializer for list views. Drops steps/options/paths/error
    message — see TaskCompactOut docstring for the rationale."""
    from vts.api.schemas import TaskCompactOut
    queue_position: int | None = None
    if queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    return TaskCompactOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        queue_position=queue_position,
        failure_code=classify_failure_code(task.error_message),
        created_at=task.created_at,
        updated_at=task.updated_at,
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
        stats=_task_stats_for_serialization(task),
    )


def _resolve_session_secret(*, env_secret: str | None, secret_file: Path) -> str:
    """Resolve the SessionMiddleware HMAC key.

    Priority:
      1. VTS_SESSION_SECRET env (explicit / HA / multi-host deployments).
      2. Contents of secret_file. Auto-created on first start so a fresh
         self-hosted install does not require manual key generation.

    On first start the file is written with mode 0600 via O_EXCL so
    parallel uvicorn workers cannot both write — the loser of the race
    catches FileExistsError and reads what the winner wrote.
    """
    if env_secret:
        return env_secret

    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(secret_file), flags, 0o600)
    except FileExistsError:
        # Another worker won the race; read its value.
        return secret_file.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_secret)
    except Exception:
        # On any write failure, remove the half-written file so the next
        # start retries cleanly rather than reading an empty secret.
        try:
            secret_file.unlink()
        except OSError:
            pass
        raise
    logging.getLogger(__name__).info(
        "generated new session secret at %s", secret_file
    )
    return new_secret


def _install_custom_openapi(app: FastAPI, settings: Settings) -> None:
    """Override app.openapi() so the generated spec is suitable for
    external clients (e.g. GPT Custom Actions, curl/Postman).

    On top of FastAPI's auto-generated spec we add:
      - `servers` with the deployment's public base URL (if configured)
      - `securitySchemes.ApiToken` (HTTP Bearer) + global default security
      - Per-path tags grouped by URL prefix (tasks, meta, admin)
    """
    from fastapi.openapi.utils import get_openapi

    def _tag_for_path(path: str) -> str:
        if path.startswith("/api/tasks"):
            return "tasks"
        if path.startswith("/api/admin"):
            return "admin"
        return "meta"

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        if settings.public_base_url:
            schema["servers"] = [{"url": settings.public_base_url.rstrip("/")}]
        schema.setdefault("components", {})["securitySchemes"] = {
            "ApiToken": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Personal API token issued from the VTS UI "
                    "(header → key icon → Create token). Format: `vts_<43 chars>`. "
                    "Browser session cookies also work for the same endpoints but "
                    "are out of scope for external clients."
                ),
            }
        }
        # Apply globally; unauthenticated endpoints opt out individually below.
        schema["security"] = [{"ApiToken": []}]
        for path, methods in schema.get("paths", {}).items():
            tag = _tag_for_path(path)
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                op.setdefault("tags", [tag])
        # Endpoints that must NOT require auth in the spec.
        for path in ("/api/version", "/healthz"):
            for op in schema.get("paths", {}).get(path, {}).values():
                if isinstance(op, dict):
                    op["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings_dep()

    if settings.oauth_enabled:
        if not settings.oauth_client_secret:
            raise RuntimeError(
                "oauth_enabled=True but oauth_client_secret is missing — "
                "set VTS_OAUTH_CLIENT_SECRET"
            )
        session_secret = _resolve_session_secret(
            env_secret=settings.session_secret,
            secret_file=settings.session_secret_file,
        )

    # Build the MCP sub-app eagerly so we can chain its lifespan into ours;
    # FastAPI does not run lifespans of mounted sub-apps, and the FastMCP
    # streamable-http transport initialises its session manager only via
    # that lifespan.
    mcp_app = None
    mcp_oauth_routes: list = []
    if settings.mcp_enabled:
        from vts.mcp import build_mcp_app_with_wellknown
        mcp_app, mcp_oauth_routes = build_mcp_app_with_wellknown(settings.mcp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            await app.state.redis.aclose()

    app = FastAPI(
        title="vts",
        version=__version__,
        description=(
            "Self-hosted video transcription and summarisation API. "
            "Authenticate with a personal API token from the VTS web UI "
            "(header → key icon → Create token). "
            "Send it as `Authorization: Bearer vts_…`. "
            "See https://github.com/gorynychzmey/vts/blob/main/docs/AUTH.md "
            "for the full auth model and "
            "https://github.com/gorynychzmey/vts/blob/main/docs/API.md "
            "for programmatic-access details (incl. GPT Custom Actions)."
        ),
        lifespan=lifespan,
    )
    _install_custom_openapi(app, settings)

    if settings.oauth_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            session_cookie="vts_session",
            https_only=True,
            same_site="lax",
            max_age=settings.session_max_age_days * 86_400,
        )

    if settings.oauth_enabled:
        from vts.api.auth_routes import router as auth_router
        app.include_router(auth_router)

    # FastMCP's OAuth routes (/.well-known/oauth-*, /authorize, /token,
    # /register, /consent, /<mcp_path>/auth/callback) all live at host
    # root per RFC 8414/9728. Mount them on the parent FastAPI BEFORE the
    # MCP sub-app so they win path matching.
    for route in mcp_oauth_routes:
        app.router.routes.append(route)

    static_dir = Path(__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if mcp_app is not None:
        app.mount(settings.mcp_path, mcp_app)

    no_cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        if settings.oauth_enabled:
            session_data = getattr(request, "session", None) or {}
            if not isinstance(session_data, dict):
                session_data = {}
            # vts-pa9: prefer sid (current cookie shape); fall back to
            # legacy email (cookies issued before vts-pa9). Either presence
            # means the user has a session — the resolver will validate it
            # on the next authenticated call.
            has_session = bool(
                (session_data.get("sid") or "").strip()
                or (session_data.get("email") or "").strip()
            )
            if not has_session:
                import urllib.parse
                return RedirectResponse(
                    url=f"/auth/login?next={urllib.parse.quote(request.url.path, safe='')}",
                    status_code=302,
                )
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        content = template.replace("__VTS_VERSION__", __version__)
        return HTMLResponse(content=content, headers=no_cache_headers)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            path=str(static_dir / "manifest.webmanifest"),
            media_type="application/manifest+json",
        )

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        # Serve service worker from root so its scope covers the whole app.
        return FileResponse(
            path=str(static_dir / "sw.js"),
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.post("/share", include_in_schema=False)
    async def share_target_post() -> RedirectResponse:
        # POST /share is normally intercepted by the service worker, which
        # stashes any shared file and redirects the client. If the SW isn't
        # active yet (first launch after install), fall back to the root so
        # the user at least lands in the app.
        return RedirectResponse(url="/?share_error=sw_not_ready", status_code=303)

    @app.get("/share", include_in_schema=False)
    async def share_target(
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
    ) -> RedirectResponse:
        # Android share sheet passes arbitrary payloads. YouTube typically
        # puts the URL into `text`. Forward everything and let the frontend
        # pick the best candidate.
        params: dict[str, str] = {}
        if url:
            params["share_url"] = url
        if text:
            params["share_text"] = text
        if title:
            params["share_title"] = title
        query = f"?{urlencode(params)}" if params else ""
        return RedirectResponse(url=f"/{query}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/version")
    async def version() -> JSONResponse:
        return JSONResponse({"version": __version__}, headers=no_cache_headers)

    @app.get("/api/me", response_model=MeOut)
    async def me(user: AuthenticatedUser = Depends(get_current_user)) -> MeOut:
        return MeOut(requested_by=user.requested_by, acting_as=user.acting_as, is_admin=user.is_admin)

    @app.get("/api/me/tokens", response_model=list[ApiTokenOut], include_in_schema=False)
    async def list_tokens(
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> list[ApiTokenOut]:
        from vts.db.repo import Repo as _Repo
        repo = _Repo(session)
        rows = await repo.list_api_tokens(uuid.UUID(user.id))
        return [
            ApiTokenOut(
                id=r.id, name=r.name, prefix=r.prefix,
                created_at=r.created_at, last_used_at=r.last_used_at,
            )
            for r in rows
        ]

    @app.post(
        "/api/me/tokens",
        response_model=ApiTokenCreateOut,
        dependencies=[Depends(require_same_site)],
        include_in_schema=False,
    )
    async def create_token(
        payload: ApiTokenCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> ApiTokenCreateOut:
        from vts.db.repo import Repo as _Repo
        from vts.services.api_tokens import generate_token, hash_token, token_prefix
        raw = generate_token()
        repo = _Repo(session)
        row = await repo.create_api_token(
            user_id=uuid.UUID(user.id),
            name=payload.name.strip(),
            token_hash=hash_token(raw),
            prefix=token_prefix(raw),
        )
        await session.commit()
        return ApiTokenCreateOut(
            id=row.id, name=row.name, prefix=row.prefix,
            created_at=row.created_at, last_used_at=None, token=raw,
        )

    @app.delete(
        "/api/me/tokens/{token_id}",
        status_code=204,
        dependencies=[Depends(require_same_site)],
        include_in_schema=False,
    )
    async def revoke_token(
        token_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        from vts.db.repo import Repo as _Repo
        repo = _Repo(session)
        ok = await repo.revoke_api_token(uuid.UUID(user.id), token_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Token not found")
        await session.commit()
        return Response(status_code=204)

    @app.get("/api/push/config", response_model=PushConfigOut, include_in_schema=False)
    async def push_config(settings: Settings = Depends(get_settings_dep)) -> PushConfigOut:
        if not is_push_enabled(settings):
            return PushConfigOut(enabled=False, public_key=None)
        return PushConfigOut(enabled=True, public_key=settings.vapid_public_key)

    @app.get("/api/push/status", response_model=PushStatusOut, include_in_schema=False)
    async def push_status(
        endpoint: str | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PushStatusOut:
        subs = await list_subscriptions(session, uuid.UUID(user.id))
        if endpoint:
            match = next((s for s in subs if s.endpoint == endpoint), None)
            return PushStatusOut(subscribed=match is not None, endpoint=endpoint if match else None)
        first = subs[0] if subs else None
        return PushStatusOut(subscribed=first is not None, endpoint=first.endpoint if first else None)

    @app.post("/api/push/subscribe", response_model=PushStatusOut, include_in_schema=False)
    async def push_subscribe(
        payload: PushSubscriptionIn,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        settings: Settings = Depends(get_settings_dep),
    ) -> PushStatusOut:
        if not is_push_enabled(settings):
            raise HTTPException(status_code=503, detail="Push notifications are not configured")
        await upsert_subscription(
            session,
            uuid.UUID(user.id),
            SubscriptionPayload(
                endpoint=payload.endpoint,
                p256dh=payload.p256dh,
                auth=payload.auth,
                user_agent=payload.user_agent,
            ),
        )
        return PushStatusOut(subscribed=True, endpoint=payload.endpoint)

    @app.post("/api/push/unsubscribe", response_model=PushStatusOut, include_in_schema=False)
    async def push_unsubscribe(
        payload: PushUnsubscribeIn,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PushStatusOut:
        await delete_subscription(session, payload.endpoint)
        return PushStatusOut(subscribed=False, endpoint=None)

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
        await bus.notify_queued()
        await bus.publish_event(
            user_id=str(task.user_id),
            task_id=str(task.id),
            event="task_status",
            data={"status": task.status.value},
        )
        set_committed_value(task, "steps", [])
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    _ALLOWED_UPLOAD_SUFFIXES = frozenset(
        {
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
            ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
        }
    )

    @app.post("/api/tasks/upload", response_model=TaskOut)
    async def upload_task(
        file: UploadFile = File(...),
        language: str | None = Form(default=None),
        audio_only: bool = Form(default=False),
        transcript: bool = Form(default=True),
        summary: bool = Form(default=True),
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        if summary and not transcript:
            raise HTTPException(status_code=422, detail="summary requires transcript")
        original_filename = file.filename or "upload"
        suffix = Path(original_filename).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix or '(none)'}")

        repo = Repo(session)
        effective_user_id = uuid.UUID(user.id)
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
            "audio_only": audio_only,
            "transcript": transcript,
            "summary": summary,
        }
        task = await repo.create_task(
            user_id=effective_user_id,
            source_url=source_url,
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
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.get(
        "/api/tasks",
        response_model=list[TaskOut] | list[TaskCompactOut],
    )
    async def list_tasks(
        limit: int | None = None,
        offset: int = 0,
        compact: bool = False,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> list[TaskOut] | list[TaskCompactOut]:
        """List tasks owned by the current user, newest first.

        Query params (added for external clients with small response budgets,
        e.g. ChatGPT Custom Actions which cap responses at ~30KB):
          - `limit`: maximum number of tasks to return (default: all).
          - `offset`: skip the first N tasks; combine with `limit` to paginate.
          - `compact`: when true, return slim `TaskCompactOut` records
            (no steps, no options, no paths). Roughly an order of magnitude
            smaller per task than the full TaskOut.
        """
        if limit is not None and limit < 0:
            raise HTTPException(status_code=422, detail="limit must be non-negative")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        if limit is not None and limit > 500:
            raise HTTPException(status_code=422, detail="limit must be <= 500")
        repo = Repo(session)
        tasks = await repo.list_tasks_for_user(
            uuid.UUID(user.id), limit=limit, offset=offset,
        )
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        task_ids = [task.id for task in tasks]
        asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
        summary_progress = {task.id: summary_progress_for_task(task) for task in tasks}
        if compact:
            return [serialize_task_compact(task, queue_positions, asr_progress, summary_progress) for task in tasks]
        return [serialize_task(task, queue_positions, asr_progress, summary_progress) for task in tasks]

    @app.get("/api/tasks/queue-positions", include_in_schema=False)
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
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.post("/api/tasks/restart_summary", response_model=BatchResultOut)
    async def restart_summary_tasks(
        request: RestartSummaryRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids, load_steps=True)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        bus = RedisBus(redis, settings)
        artifact_resets: list[asyncio.Task[None]] = []
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            if request.mode == "final_only":
                if not can_restart_final_summary_task(task):
                    results[tid] = f"cannot_restart_final:{task.status.value}"
                    continue
                _reset_final_summary_step(task)
                artifact_resets.append(asyncio.to_thread(_reset_final_summary_artifacts, task))
            else:
                if not can_restart_summary_task(task):
                    results[tid] = f"cannot_restart:{task.status.value}"
                    continue
                _reset_summary_steps(task)
                artifact_resets.append(asyncio.to_thread(_reset_summary_artifacts, task))
            task.summary_path = None
            await repo.set_task_summary_progress(task, 0, 0)
            await repo.set_task_status(task, TaskStatus.queued)
            results[tid] = "queued"
        await asyncio.gather(*artifact_resets)
        await session.commit()
        if any(v == "queued" for v in results.values()):
            await bus.notify_queued()
        return BatchResultOut(results=results)

    @app.post("/api/tasks/pause", response_model=BatchResultOut)
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
            await bus.clear_pause_request(task_id)
            await repo.set_task_status(task, TaskStatus.queued)
            results[tid] = "queued"
        await session.commit()
        if any(v == "queued" for v in results.values()):
            await bus.notify_queued()
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

    @app.get(
        "/api/tasks/{task_id}/transcript",
        responses={
            200: {
                "description": "Raw transcript. text/plain when the artifact is a .txt file, application/json otherwise.",
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"type": "object"}},
                },
            },
            404: {"description": "Task or transcript artifact not found"},
        },
    )
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

    @app.get(
        "/api/tasks/{task_id}/summary",
        responses={
            200: {
                "description": "Markdown summary. text/markdown when the artifact is .md, application/json otherwise.",
                "content": {
                    "text/markdown": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"type": "object"}},
                },
            },
            404: {"description": "Task or summary artifact not found"},
        },
    )
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

    @app.get(
        "/api/tasks/{task_id}/redacted",
        responses={
            200: {
                "description": "Redacted plain-text transcript.",
                "content": {"text/plain": {"schema": {"type": "string"}}},
            },
            404: {"description": "Task or redacted transcript not found"},
        },
    )
    async def get_redacted_transcript(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
        return Response(content=path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    @app.get(
        "/api/tasks/{task_id}/log",
        responses={
            200: {
                "description": "Plain-text task log. Empty body if the task has no log yet.",
                "content": {"text/plain": {"schema": {"type": "string"}}},
            },
            404: {"description": "Task not found"},
        },
    )
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

    @app.get("/api/tasks/{task_id}/media")
    async def get_media(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> FileResponse:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        media_file = _find_media_file(task.artifact_dir)
        if media_file is None:
            raise HTTPException(status_code=404, detail="Media file not available")
        return FileResponse(
            path=str(media_file),
            filename=media_file.name,
            media_type=media_content_type(media_file),
        )

    @app.get("/player/{task_id}", include_in_schema=False, response_class=HTMLResponse)
    async def media_player(
        task_id: uuid.UUID,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> HTMLResponse:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        media_file = _find_media_file(task.artifact_dir)
        if media_file is None:
            raise HTTPException(status_code=404, detail="Media file not available")
        kind = media_kind(media_file)
        # source_url is "file://<name>" for uploads, an http URL otherwise;
        # in either case the last path segment is a sensible display name.
        title = (task.source_url or "").rsplit("/", 1)[-1] or media_file.name
        # Propagate admin impersonation: <video>/<audio> will fire its own
        # request to /api/tasks/<id>/media, which must resolve to the same
        # acting user as the page itself — otherwise the request resolves
        # as the admin and the task ownership check returns 404.
        src = f"/api/tasks/{task_id}/media"
        acting_as = request.query_params.get("as_user")
        if acting_as:
            src = f"{src}?{urlencode({'as_user': acting_as})}"
        tag = (
            f'<video controls autoplay src="{_html.escape(src, quote=True)}"></video>'
            if kind == "video"
            else f'<audio controls autoplay src="{_html.escape(src, quote=True)}"></audio>'
        )
        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #111; color: #ddd;
    font-family: system-ui, sans-serif; min-height: 100vh; }}
  body {{ display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 1rem; }}
  h1 {{ font-size: 1rem; font-weight: 400; margin: 0 0 1rem;
    word-break: break-all; text-align: center; }}
  video, audio {{ max-width: 100%; width: min(960px, 100%); }}
  video {{ max-height: 80vh; background: #000; }}
</style>
</head>
<body>
<h1>{_html.escape(title)}</h1>
{tag}
</body>
</html>"""
        return HTMLResponse(html)

    @app.get("/api/events", include_in_schema=False)
    async def get_events(
        user: AuthenticatedUser = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> StreamingResponse:
        async def event_generator() -> Any:
            yield f"event: server_version\ndata: {json.dumps({'version': __version__}, ensure_ascii=True)}\n\n"
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
