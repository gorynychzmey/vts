"""MCP tools over delivery credentials, targets and per-task delivery status."""

from __future__ import annotations

import uuid
from typing import Any

from fastmcp import FastMCP

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import CREATE, DELIVER, DESTRUCTIVE, READ_ONLY, UPDATE
from vts.mcp.schemas import (
    DeliveryCredentialInfo,
    DeliveryStatusInfo,
    DeliveryTargetInfo,
)
from vts.mcp.tools import (
    create_delivery_credential,
    create_delivery_target,
    delete_delivery_credential,
    delete_delivery_target,
    get_delivery_status,
    list_delivery_credentials,
    list_delivery_targets,
    retry_delivery,
    update_delivery_credential,
    update_delivery_target,
)


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""
    @mcp.tool(name="list_delivery_targets", annotations=READ_ONLY)
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

    @mcp.tool(name="list_delivery_credentials", annotations=READ_ONLY)
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

    @mcp.tool(name="create_delivery_credential", annotations=CREATE)
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

    @mcp.tool(name="update_delivery_credential", annotations=UPDATE)
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

    @mcp.tool(name="delete_delivery_credential", annotations=DESTRUCTIVE)
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

    @mcp.tool(name="create_delivery_target", annotations=CREATE)
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

    @mcp.tool(name="update_delivery_target", annotations=UPDATE)
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

    @mcp.tool(name="delete_delivery_target", annotations=DESTRUCTIVE)
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

    @mcp.tool(name="get_delivery_status", annotations=READ_ONLY)
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

    @mcp.tool(name="retry_delivery", annotations=DELIVER)
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
