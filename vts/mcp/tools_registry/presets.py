"""MCP tools over presets, including the caller's default preset."""

from __future__ import annotations

import uuid
from typing import Any

from fastmcp import FastMCP

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import CREATE, DESTRUCTIVE, READ_ONLY, UPDATE
from vts.mcp.schemas import PresetInfo
from vts.mcp.tools import (
    create_preset,
    delete_preset,
    get_default_preset,
    list_presets,
    set_default_preset,
    update_preset,
)


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""
    @mcp.tool(name="list_presets", annotations=READ_ONLY)
    async def _list_presets() -> list[PresetInfo]:
        """List presets available to the caller (system + user-defined)."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_presets(user=user, repo=Repo(session))

    @mcp.tool(name="create_preset", annotations=CREATE)
    async def _create_preset(name: str, options: dict) -> PresetInfo:
        """Create a user-defined preset. options is a pipeline-options dict
        (language, audio_only, transcript, prompts). Returns the new preset's info."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await create_preset(name=name, options=options, user=user, repo=Repo(session))
            await session.commit()
            return result

    @mcp.tool(name="update_preset", annotations=UPDATE)
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

    @mcp.tool(name="delete_preset", annotations=DESTRUCTIVE)
    async def _delete_preset(preset_id: uuid.UUID) -> dict[str, Any]:
        """Delete a user-defined preset."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await delete_preset(preset_id=preset_id, user=user, repo=Repo(session))
            await session.commit()
            return result

    @mcp.tool(name="get_default_preset", annotations=READ_ONLY)
    async def _get_default_preset() -> dict[str, Any]:
        """Return the caller's default preset ref (system default if unset)."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await get_default_preset(user=user, repo=Repo(session))

    @mcp.tool(name="set_default_preset", annotations=UPDATE)
    async def _set_default_preset(source: str, id: str) -> dict[str, Any]:
        """Set the caller's default preset to a system or user preset ref."""
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            result = await set_default_preset(source=source, id=id, user=user, repo=Repo(session))
            await session.commit()
            return result
