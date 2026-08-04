from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastmcp import FastMCP
from redis.asyncio import Redis

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import (
    DeliveryCredentialInfo,
    DeliveryStatusInfo,
    DeliveryTargetInfo,
    PresetInfo,
    PromptInfo,
    PromptResult,
    SubmitVideoResult,
    TaskPage,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.mcp.tools import (
    create_delivery_credential,
    create_delivery_target,
    create_preset,
    create_prompt,
    delete_delivery_credential,
    delete_delivery_target,
    delete_preset,
    delete_prompt,
    get_default_preset,
    get_delivery_status,
    get_prompt_result,
    get_status,
    get_transcript,
    list_delivery_credentials,
    list_delivery_targets,
    list_presets,
    list_prompts,
    list_tasks,
    retry_delivery,
    set_default_preset,
    submit_video,
    update_delivery_credential,
    update_delivery_target,
    update_preset,
    update_prompt,
    wait_for_task,
)
from vts.core.config import get_settings
from vts.services.redis_bus import RedisBus


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server with all MCP tools registered."""
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

    @mcp.tool(name="list_tasks")
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
        Keep filters identical across pages of one walk — changing a filter
        changes the set the cursor points into.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, cursor=cursor,
                q=q, created_from=created_from, created_to=created_to,
                source_type=source_type,
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

    @mcp.tool(name="list_presets")
    async def _list_presets() -> list[PresetInfo]:
        """List presets available to the caller (system + user-defined)."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_presets(user=user, repo=Repo(session))

    @mcp.tool(name="create_preset")
    async def _create_preset(name: str, options: dict) -> PresetInfo:
        """Create a user-defined preset. options is a pipeline-options dict
        (language, audio_only, transcript, prompts). Returns the new preset's info."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await create_preset(name=name, options=options, user=user, repo=Repo(session))
            await session.commit()
            return result

    @mcp.tool(name="update_preset")
    async def _update_preset(
        preset_id: uuid.UUID, name: str | None = None, options: dict | None = None
    ) -> PresetInfo:
        """Update a user-defined preset's name and/or options."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await update_preset(
                preset_id=preset_id, name=name, options=options, user=user, repo=Repo(session),
            )
            await session.commit()
            return result

    @mcp.tool(name="delete_preset")
    async def _delete_preset(preset_id: uuid.UUID) -> dict[str, Any]:
        """Delete a user-defined preset."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await delete_preset(preset_id=preset_id, user=user, repo=Repo(session))
            await session.commit()
            return result

    @mcp.tool(name="list_delivery_targets")
    async def _list_delivery_targets() -> list[DeliveryTargetInfo]:
        """List the caller's delivery targets.

        Secret values are never returned — each secret shows only whether it is
        set. `adapter_available` is False when the target's adapter plugin is
        not installed right now; such a target cannot be used for a new task
        until it is back.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            return await list_delivery_targets(user=user, repo=Repo(session), settings=settings)

    @mcp.tool(name="list_delivery_credentials")
    async def _list_delivery_credentials() -> list[DeliveryCredentialInfo]:
        """List the caller's delivery connections.

        A connection holds an endpoint and its credentials; several targets can
        share one, so rotating a token is a single edit. Secret values are never
        returned — each shows only whether it is set. `used_by` counts the
        targets referencing it.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            return await list_delivery_credentials(
                user=user, repo=Repo(session), settings=settings
            )

    @mcp.tool(name="create_delivery_credential")
    async def _create_delivery_credential(
        name: str, adapter: str, config: dict | None = None,
        secrets: dict[str, str] | None = None,
    ) -> DeliveryCredentialInfo:
        """Create a connection: where to deliver and who to authenticate as.

        Args:
            name: Unique name for this connection, for humans to recognise it.
            adapter: Installed adapter to deliver through (e.g. "outline").
            config: Non-secret connection settings (e.g. base_url).
            secrets: Sensitive values (e.g. {"api_token": "..."}). Encrypted at
                rest and never returned by any tool.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            result = await create_delivery_credential(
                user=user, repo=Repo(session), settings=settings,
                name=name, adapter=adapter, config=config, secrets=secrets,
            )
            await session.commit()
            return result

    @mcp.tool(name="update_delivery_credential")
    async def _update_delivery_credential(
        credential_id: str, name: str | None = None, config: dict | None = None,
        secrets: dict[str, str] | None = None, clear_secrets: bool = False,
    ) -> DeliveryCredentialInfo:
        """Update a connection. Omitting `secrets` keeps the stored ones;
        pass clear_secrets=True to remove them."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            result = await update_delivery_credential(
                user=user, repo=Repo(session), settings=settings,
                credential_id=credential_id, name=name, config=config,
                secrets=secrets, clear_secrets=clear_secrets,
            )
            await session.commit()
            return result

    @mcp.tool(name="delete_delivery_credential")
    async def _delete_delivery_credential(credential_id: str) -> dict[str, Any]:
        """Delete a connection. Refused while targets still reference it."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await delete_delivery_credential(
                user=user, repo=Repo(session), credential_id=credential_id
            )
            await session.commit()
            return result

    @mcp.tool(name="create_delivery_target")
    async def _create_delivery_target(
        name: str, adapter: str, credential_id: str, config: dict | None = None,
    ) -> DeliveryTargetInfo:
        """Create a delivery target: one destination for task results.

        Args:
            name: Unique name for this target, for humans to recognise it.
                Tasks reference the target by its ID, not this name, so
                renaming it later never breaks anything.
            adapter: Installed adapter to deliver through (e.g. "outline").
            credential_id: ID of the connection to deliver through, from
                list_delivery_credentials. Required — the endpoint and its
                secrets always live on a connection, never on the target.
            config: Per-destination settings (e.g. collection_id,
                default_variant). Connection settings belong on the credential.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            result = await create_delivery_target(
                user=user, repo=Repo(session), settings=settings,
                name=name, adapter=adapter, credential_id=credential_id, config=config,
            )
            await session.commit()
            return result

    @mcp.tool(name="update_delivery_target")
    async def _update_delivery_target(
        target_id: str, name: str | None = None, config: dict | None = None,
        credential_id: str | None = None,
    ) -> DeliveryTargetInfo:
        """Update a delivery target. Pass credential_id to point it at another
        connection; secrets are managed through the connection itself."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            result = await update_delivery_target(
                user=user, repo=Repo(session), settings=settings, target_id=target_id,
                name=name, config=config, credential_id=credential_id,
            )
            await session.commit()
            return result

    @mcp.tool(name="delete_delivery_target")
    async def _delete_delivery_target(target_id: str) -> dict[str, Any]:
        """Delete a delivery target."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await delete_delivery_target(
                user=user, repo=Repo(session), target_id=target_id
            )
            await session.commit()
            return result

    @mcp.tool(name="get_delivery_status")
    async def _get_delivery_status(task_id: uuid.UUID) -> list[DeliveryStatusInfo]:
        """Where each of a task's deliveries got to.

        Poll this when you need to know the result actually landed before
        continuing a pipeline: `delivered` with an `external_url` is the
        confirmation. `waiting_for_adapter` means the destination's plugin is
        temporarily missing — the delivery is queued, not lost, and leaves on
        its own once the plugin is back.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_delivery_status(user=user, repo=Repo(session), task_id=task_id)

    @mcp.tool(name="retry_delivery")
    async def _retry_delivery(task_id: uuid.UUID, target_id: str | None = None) -> dict[str, Any]:
        """Retry a task's dead deliveries (optionally just one target's).

        Returns how many were revived. Deliveries waiting on a missing adapter
        are left alone — they retry themselves when the plugin returns.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await retry_delivery(
                user=user, repo=Repo(session), task_id=task_id, target_id=target_id
            )
            await session.commit()
            return result

    @mcp.tool(name="get_default_preset")
    async def _get_default_preset() -> dict[str, Any]:
        """Return the caller's default preset ref (system default if unset)."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_default_preset(user=user, repo=Repo(session))

    @mcp.tool(name="set_default_preset")
    async def _set_default_preset(source: str, id: str) -> dict[str, Any]:
        """Set the caller's default preset to a system or user preset ref."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await set_default_preset(source=source, id=id, user=user, repo=Repo(session))
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
