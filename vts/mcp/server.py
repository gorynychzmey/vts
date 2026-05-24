from __future__ import annotations

import uuid
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from redis.asyncio import Redis

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import (
    SubmitVideoResult,
    SummaryResult,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.mcp.tools import (
    get_status,
    get_summary,
    get_transcript,
    list_tasks,
    submit_video,
    wait_for_task,
)
from vts.services.redis_bus import RedisBus


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server with all six MCP tools registered."""
    mcp = FastMCP(name="vts")

    @mcp.tool(name="submit_video")
    async def _submit_video(url: str) -> SubmitVideoResult:
        """Submit a video URL for processing. Returns task_id immediately."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(get_http_request(), session)
            repo = Repo(session)
            redis = Redis.from_url(settings.redis_url, decode_responses=False)
            try:
                bus = RedisBus(redis, settings)
                result = await submit_video(
                    url=url, user=user, repo=repo, bus=bus,
                    artifacts_root=settings.artifacts_root,
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
            user, _settings = await mcp_authenticate(get_http_request(), session)
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, sort=sort, order=order,
            )

    @mcp.tool(name="get_status")
    async def _get_status(task_id: uuid.UUID) -> TaskStatusResult:
        """Get current pipeline status for one task."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(get_http_request(), session)
            return await get_status(task_id=task_id, user=user, repo=Repo(session))

    @mcp.tool(name="get_transcript")
    async def _get_transcript(
        task_id: uuid.UUID, variant: Literal["raw", "redacted"] = "raw"
    ) -> TranscriptResult:
        """Fetch the transcript text. variant=raw is the ASR output, variant=redacted is the processed version."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(get_http_request(), session)
            return await get_transcript(task_id=task_id, variant=variant, user=user, repo=Repo(session))

    @mcp.tool(name="get_summary")
    async def _get_summary(task_id: uuid.UUID) -> SummaryResult:
        """Fetch the markdown summary for a task."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(get_http_request(), session)
            return await get_summary(task_id=task_id, user=user, repo=Repo(session))

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
            user, settings = await mcp_authenticate(get_http_request(), auth_session)
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


def build_mcp_app() -> Any:
    # path="/" mounts the streamable-HTTP endpoint at the sub-app root.
    # Combined with `app.mount(settings.mcp_path, ...)` this exposes the
    # transport at the configured path itself (e.g. /mcp) instead of the
    # nested /mcp/mcp that FastMCP's default path="/mcp" would produce.
    return build_mcp_server().http_app(path="/")
