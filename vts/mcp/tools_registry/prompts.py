"""MCP tools over user-defined prompts."""

from __future__ import annotations

import uuid
from typing import Any

from fastmcp import FastMCP

from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import PromptInfo
from vts.mcp.tools import create_prompt, delete_prompt, list_prompts, update_prompt


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""
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
