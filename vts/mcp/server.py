from __future__ import annotations

import uuid
from typing import Any, Literal

from fastmcp import FastMCP
from redis.asyncio import Redis

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import (
    PromptInfo,
    PromptResult,
    SubmitVideoResult,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.mcp.tools import (
    create_prompt,
    delete_prompt,
    get_prompt_result,
    get_status,
    get_transcript,
    list_prompts,
    list_tasks,
    submit_video,
    update_prompt,
    wait_for_task,
)
from vts.core.config import get_settings
from vts.services.redis_bus import RedisBus


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server with all ten MCP tools registered."""
    settings = get_settings()
    auth_provider = None
    if settings.oauth_enabled:
        from fastmcp.server.auth.providers.google import GoogleProvider

        if not settings.oauth_client_id or not settings.oauth_client_secret:
            raise RuntimeError(
                "oauth_enabled but client_id/client_secret missing — "
                "set VTS_OAUTH_CLIENT_ID and VTS_OAUTH_CLIENT_SECRET"
            )
        if not settings.public_base_url:
            raise RuntimeError(
                "oauth_enabled but public_base_url missing — "
                "set VTS_PUBLIC_BASE_URL (e.g. https://vts.example.com)"
            )
        # FastMCP's auth provider publishes /.well-known/oauth-* metadata
        # whose URLs are anchored to issuer_url's host (RFC 8414/9728: metadata
        # MUST live at the host root, not under a subpath). When the MCP app
        # is mounted at /mcp the well-known routes also need to be reachable
        # at the host root — see build_mcp_app() below, which extracts them
        # so the parent FastAPI can mount them on /.
        #
        # base_url stays host-only (no /mcp suffix): that's what the spec
        # calls the "resource server URL" and what well-known docs reference.
        # redirect_path is moved off /auth/callback (used by the web UI) to
        # /mcp/auth/callback, which is what the Google client already has
        # registered for MCP.
        auth_provider = GoogleProvider(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            base_url=settings.public_base_url.rstrip("/"),
            redirect_path=f"{settings.mcp_path.rstrip('/')}/auth/callback",
            required_scopes=["openid", "email"],
            require_authorization_consent="remember",
        )
    mcp = FastMCP(name="vts", auth=auth_provider)

    @mcp.tool(name="submit_video")
    async def _submit_video(
        url: str,
        language: str | None = None,
        audio_only: bool = False,
        transcript: bool = True,
        prompts: list[dict] | None = None,
    ) -> SubmitVideoResult:
        """Submit a video URL for processing. Returns task_id immediately.

        Args:
            url: Video URL (yt-dlp supported sources).
            language: Optional ISO language code (e.g. "en", "ru") to skip
                language autodetection. Default: autodetect.
            audio_only: Download audio track only, skip video. Default: False.
            transcript: Run ASR transcription. Default: True. Set False to
                skip transcription entirely (audio/video download only).
            prompts: Prompts to run against the transcript, each a ref like
                {"source": "system", "id": "summary"} or
                {"source": "user", "id": "<prompt-uuid>"}. Defaults to the
                single system "summary" prompt. Non-empty prompts require
                transcript=True (rejected with 422 otherwise).
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            repo = Repo(session)
            redis = Redis.from_url(settings.redis_url, decode_responses=False)
            try:
                bus = RedisBus(redis, settings)
                result = await submit_video(
                    url=url, user=user, repo=repo, bus=bus,
                    artifacts_root=settings.artifacts_root,
                    language=language,
                    audio_only=audio_only,
                    transcript=transcript,
                    prompts=prompts,
                )
                await session.commit()
                return result
            finally:
                await redis.aclose()

    @mcp.tool(name="list_tasks")
    async def _list_tasks(
        status: Literal[
            "queued", "running", "paused", "completed", "archived", "failed", "canceled"
        ] | None = None,
        limit: int = 20,
        sort: Literal["created_at", "updated_at", "title"] = "updated_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[TaskSummary]:
        """List tasks owned by the calling user."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, sort=sort, order=order,
            )

    @mcp.tool(name="get_status")
    async def _get_status(task_id: uuid.UUID) -> TaskStatusResult:
        """Get current pipeline status for one task."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_status(task_id=task_id, user=user, repo=Repo(session))

    @mcp.tool(name="get_transcript")
    async def _get_transcript(
        task_id: uuid.UUID, variant: Literal["raw", "redacted"] = "raw"
    ) -> TranscriptResult:
        """Fetch the transcript text. variant=raw is the ASR output, variant=redacted is the processed version."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_transcript(task_id=task_id, variant=variant, user=user, repo=Repo(session))

    @mcp.tool(name="get_prompt_result")
    async def _get_prompt_result(task_id: uuid.UUID, ref: str = "system:summary") -> PromptResult:
        """Fetch the rendered text for one prompt result of a task.

        ref is a "source:id" string, e.g. "system:summary" (the default,
        which returns the markdown summary) or "user:<prompt-uuid>".
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_prompt_result(task_id=task_id, ref=ref, user=user, repo=Repo(session))

    @mcp.tool(name="list_prompts")
    async def _list_prompts() -> list[PromptInfo]:
        """List prompts available to the caller (system + user-defined)."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_prompts(user=user, repo=Repo(session))

    @mcp.tool(name="create_prompt")
    async def _create_prompt(name: str, system_prompt: str) -> PromptInfo:
        """Create a user-defined prompt. Returns the new prompt's info."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await create_prompt(
                name=name, system_prompt=system_prompt, user=user, repo=Repo(session)
            )
            await session.commit()
            return result

    @mcp.tool(name="update_prompt")
    async def _update_prompt(
        prompt_id: uuid.UUID, name: str | None = None, system_prompt: str | None = None
    ) -> PromptInfo:
        """Update a user-defined prompt's name and/or body."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await update_prompt(
                prompt_id=prompt_id, name=name, system_prompt=system_prompt,
                user=user, repo=Repo(session),
            )
            await session.commit()
            return result

    @mcp.tool(name="delete_prompt")
    async def _delete_prompt(prompt_id: uuid.UUID) -> dict[str, Any]:
        """Delete a user-defined prompt."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await delete_prompt(prompt_id=prompt_id, user=user, repo=Repo(session))
            await session.commit()
            return result

    @mcp.tool(name="wait_for_task")
    async def _wait_for_task(
        task_id: uuid.UUID,
        until: Literal["transcript", "summary", "done"] = "done",
        timeout_seconds: int = 300,
    ) -> WaitResult:
        """Block until the task reaches the target stage or the timeout fires."""
        # We resolve the user first inside a short-lived session, then release it
        # before opening Redis — the wait can block for up to 30 min and we don't
        # want to hold a DB connection that whole time.
        session_factory = get_db_session_factory()
        async with session_factory() as auth_session:
            user, settings = await mcp_authenticate(auth_session)
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            async with session_factory() as session:
                return await wait_for_task(
                    task_id=task_id, until=until, timeout_seconds=timeout_seconds,
                    user=user, repo=Repo(session), redis=redis,
                    events_channel=f"{settings.redis_prefix}events",
                )
        finally:
            await redis.aclose()

    return mcp


def build_mcp_app_with_wellknown(mcp_path: str) -> tuple[Any, list]:
    """Build the ASGI app AND extract the FastMCP auth provider's
    OAuth routes that must live at host root.

    RFC 8414 + RFC 9728 require OAuth metadata to live at the resource's
    host root, not under a subpath. The metadata document also references
    /authorize, /token, /register, /consent and the redirect callback —
    all of which must therefore live at root too, otherwise clients hit
    the URL advertised by the metadata and get 404s from sub-app paths.

    FastMCP exposes these routes via `auth.get_routes(mcp_path=...)`; we
    return them ALL so the parent FastAPI mounts them on `/`. The MCP
    sub-app itself (mounted at mcp_path) is left with the JSON-RPC
    endpoint and nothing else auth-related — auth.get_routes(...) already
    omits the streamable-HTTP transport handler.

    Returns (asgi_app, oauth_routes). oauth_routes is an empty list when
    no auth provider is attached.
    """
    server = build_mcp_server()
    # path="/" mounts the streamable-HTTP endpoint at the sub-app root so
    # the external URL is /mcp (when the sub-app is mounted at /mcp) rather
    # than /mcp/mcp.
    app = server.http_app(path="/")
    routes: list = []
    if server.auth is not None:
        routes = list(server.auth.get_routes(mcp_path=mcp_path))
    return app, routes


def build_mcp_app() -> Any:
    """Legacy single-return accessor — used by callers that don't need the
    OAuth routes (e.g. when OAuth is off)."""
    app, _ = build_mcp_app_with_wellknown(mcp_path="/mcp")
    return app
