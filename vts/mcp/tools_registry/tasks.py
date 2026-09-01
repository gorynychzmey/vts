"""MCP tools over tasks: submit, list, status, transcript, prompt results."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException
from fastmcp import FastMCP
from redis.asyncio import Redis

from vts.core.config import get_settings
from vts.api._helpers.recordings import (
    artifacts_removable_for_task,
    delete_task_with_recording,
)
from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import DESTRUCTIVE, READ_ONLY, SUBMIT
from vts.mcp.schemas import (
    PromptResult,
    SubmitVideoResult,
    TaskPage,
    TaskStatusResult,
    TranscriptResult,
    WaitResult,
)
from vts.mcp.tools import (
    get_prompt_result,
    get_status,
    get_transcript,
    list_tasks,
    submit_video,
    wait_for_task,
)
from vts.services.redis_bus import RedisBus


logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""
    @mcp.tool(name="submit_video", annotations=SUBMIT)
    async def _submit_video(
        url: str,
        language: str | None = None,
        audio_only: bool = False,
        transcript: bool = True,
        diarize: bool = False,
        prompts: list[dict] | None = None,
        preset: dict | None = None,
        delivery: list[dict] | None = None,
    ) -> SubmitVideoResult:
        """Submit a video URL for processing. Returns task_id immediately.

        Args:
            url: Video URL (yt-dlp supported sources).
            language: Optional ISO language code (e.g. "en", "ru") to skip
                language autodetection. Default: autodetect.
            audio_only: Download audio track only, skip video. Default: False.
            transcript: Run ASR transcription. Default: True. Set False to
                skip transcription entirely (audio/video download only).
            diarize: Run speaker diarization and attribute transcript lines to
                speakers. Default: False (costs a full extra pass over the
                audio). Requires transcript=True (rejected with 422 otherwise).
            prompts: Prompts to run against the transcript, each a ref like
                {"source": "system", "id": "summary"} or
                {"source": "user", "id": "<prompt-uuid>"}. Defaults to the
                single system "summary" prompt. Non-empty prompts require
                transcript=True (rejected with 422 otherwise).
            preset: Optional preset ref like {"source": "system", "id": "default"}
                or {"source": "user", "id": "<preset-uuid>"} supplying default
                pipeline options. The preset fills any field you leave at its
                default; explicit params above override the preset.
            delivery: Optional destinations for the result, each
                {"deliver_to": "<target id>", "variant": "<artifact>"}.
                Targets are managed with the delivery-target tools; reference
                them by ID (from list_delivery_targets), not by name, so that
                renaming a target never breaks a queued task. `variant` picks
                which artifact to send: "raw" is the ASR transcript,
                "redacted" the segment-prompt-polished transcript, "summary"
                the final summary, and a prompt ref like "user:<prompt-uuid>"
                the rendered output of that prompt — which then has to be
                among `prompts`, or the delivery would wait on an artifact
                nothing produces. Omitting `variant` uses the target's
                configured default. An unknown target id, or one whose adapter
                plugin is not currently installed, is rejected with 422.
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
                    diarize=diarize,
                    prompts=prompts,
                    preset=preset,
                    delivery=delivery,
                )
                await session.commit()
                return result
            finally:
                await redis.aclose()

    @mcp.tool(name="list_tasks", annotations=READ_ONLY)
    async def _list_tasks(
        status: Literal[
            "queued", "running", "paused", "completed", "archived", "failed", "canceled"
        ] | None = None,
        limit: int = 20,
        cursor: str | None = None,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        source_type: Literal["file", "url"] | None = None,
        diarized: bool | None = None,
        person: str | None = None,
    ) -> TaskPage:
        """List the calling user's tasks, newest first, in pages.

        Returns up to `limit` tasks (max 100) plus `next_cursor` and `has_more`.
        To fetch the next page, call again with `cursor` set to the
        `next_cursor` from the previous response. When `has_more` is false (or
        `next_cursor` is null) there are no more tasks.

        Filters, all optional and combinable:
            status: only tasks in this pipeline state.
            q: free text matched against the task's title AND its URL, so a
                remembered fragment of either finds the task.
            created_from / created_to: bound the creation time, inclusive.
            source_type: "file" for uploads, "url" for links.
            diarized: True for tasks with identified voices, False for those
                without.
            person: a name (any part of it, case-insensitive) — tasks where
                that person was identified by voice.
        Keep filters identical across pages of one walk — changing a filter
        changes the set the cursor points into.

        Each task carries `people`: the names identified in it, empty when
        voices were never resolved. Use `list_people` to see who is known.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, cursor=cursor,
                q=q, created_from=created_from, created_to=created_to,
                source_type=source_type, diarized=diarized, person=person,
            )

    @mcp.tool(name="delete_task", annotations=DESTRUCTIVE)
    async def _delete_task(task_id: uuid.UUID) -> dict[str, Any]:
        """Delete a task, its recording, and every file and transcript of it.

        PERMANENT and NOT recoverable. The recording produced by this task goes
        with it, including its transcript text in the search corpus — deleting
        a job must not leave the conversation behind.

        Ask the person before calling this. Do not call it to tidy up, to free
        space, or because a task looks finished or failed: only when they have
        asked for that specific task to be deleted. If they want the media gone
        but the text kept, that is `archive_task`, not this.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            repo = Repo(session)
            task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            title = task.source_title or task.source_url
            artifact_dir = task.artifact_dir
            # The same audit line the HTTP path writes, and for the same
            # reason: a 2026-08-24 incident could not be reconstructed because
            # an irreversible delete left only "DELETE /api/tasks 200 OK".
            # Through MCP there is not even that — the caller is an agent, so
            # without this line nothing records who removed what. Both
            # identities go in: acting_as alone cannot distinguish a user
            # deleting their own task from an admin impersonating them.
            logger.info(
                "task.delete via=mcp requested_by=%s acting_as=%s task_id=%s",
                user.requested_by,
                user.acting_as,
                task.id,
            )
            # Cancel before deleting: otherwise the rows and the directory go
            # out from under a worker that is still running the task. What
            # breaks depends on the stage it reached, which is precisely why
            # the HTTP path cancels first.
            redis = Redis.from_url(settings.redis_url, decode_responses=False)
            try:
                await RedisBus(redis, settings).request_cancel(task.id)
                await repo.set_task_status(task, TaskStatus.canceled)
            except Exception:  # noqa: BLE001 - cancellation is best-effort
                # The delete itself does not need the bus, so an unreachable
                # Redis must not turn "delete this task" into an error the
                # caller cannot act on. Logged loudly because the worker may
                # then still be running when the rows go.
                logger.warning(
                    "task.delete could not cancel task_id=%s before deleting; "
                    "a worker may still be running it",
                    task.id,
                    exc_info=True,
                )
            finally:
                await redis.aclose()
            # Ask BEFORE deleting the rows, while the claims are still visible:
            # another recording may point at this same directory (the detached
            # case SET NULL exists for), and removing it would destroy files
            # that recording still owns. The HTTP path guards this; the tool
            # must not be the cheaper way to lose data.
            removable = await artifacts_removable_for_task(session, task)
            await delete_task_with_recording(session, task)
            await session.commit()
            # Files go only after the rows are committed: a crash between the
            # two must not leave a row pointing at a directory that is gone.
            if artifact_dir and removable:
                shutil.rmtree(Path(artifact_dir), ignore_errors=True)
            return {"deleted": True, "task_id": str(task_id), "title": title}

    @mcp.tool(name="get_status", annotations=READ_ONLY)
    async def _get_status(task_id: uuid.UUID) -> TaskStatusResult:
        """Get current pipeline status for one task."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_status(task_id=task_id, user=user, repo=Repo(session))

    @mcp.tool(name="get_transcript", annotations=READ_ONLY)
    async def _get_transcript(
        task_id: uuid.UUID, variant: Literal["raw", "redacted"] = "raw"
    ) -> TranscriptResult:
        """Fetch the transcript text. variant=raw is the ASR output, variant=redacted is the processed version."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_transcript(task_id=task_id, variant=variant, user=user, repo=Repo(session))

    @mcp.tool(name="get_prompt_result", annotations=READ_ONLY)
    async def _get_prompt_result(task_id: uuid.UUID, ref: str = "system:summary") -> PromptResult:
        """Fetch the rendered text for one prompt result of a task.

        ref is a "source:id" string, e.g. "system:summary" (the default,
        which returns the markdown summary) or "user:<prompt-uuid>".
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_prompt_result(task_id=task_id, ref=ref, user=user, repo=Repo(session))

    @mcp.tool(name="wait_for_task", annotations=READ_ONLY)
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
