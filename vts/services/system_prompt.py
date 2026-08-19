"""Per-user copies of the vendor system prompt (vts-kujy).

The vendor's text stays in `prompts/global_prompt.md`; the database holds each
user's copy of it. Keeping the file as the only reference is what makes
restoring free — deleting the row and reading the file again is the whole
mechanism — at the cost that a copy, once made, does not follow later edits to
the file on its own. `refresh_untouched_system_prompts` closes that gap.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import Prompt
from vts.db.repo import Repo
from vts.services.prompt_registry import list_system_prompts
from vts.services.summarizer import load_prompt

_SUMMARY_KEY = "summary"
_FALLBACK = "Produce a structured knowledge document from the notes."


def vendor_text(prompts_dir: Path) -> str:
    """The vendor's own wording, straight from the file."""
    spec = next((p for p in list_system_prompts() if p.key == _SUMMARY_KEY), None)
    file = spec.file if spec is not None else "global_prompt.md"
    return load_prompt(prompts_dir, file, _FALLBACK)


def vendor_name(default: str = "Summary") -> str:
    spec = next((p for p in list_system_prompts() if p.key == _SUMMARY_KEY), None)
    return spec.display_name if spec is not None else default


async def get_or_create_system_prompt(
    session: AsyncSession, user_id: uuid.UUID, prompts_dir: Path
) -> Prompt:
    """The user's copy of the vendor prompt, made from the file if absent.

    Called from the API when the user opens the prompt list, and from the
    pipeline when a task resolves `{"source": "system", "id": "summary"}` — a
    user who has never opened the UI still has no copy when their first
    summary runs.
    """
    stmt = sa.select(Prompt).where(Prompt.user_id == user_id, Prompt.is_system)
    existing = (await session.scalars(stmt)).first()
    if existing is not None:
        return existing

    try:
        created = await Repo(session).create_prompt(
            user_id, vendor_name(), vendor_text(prompts_dir), is_system=True
        )
        await session.flush()
        return created
    except IntegrityError:
        # The partial unique index rejected us: another caller created the copy
        # between our SELECT and INSERT. That is the race resolving correctly —
        # re-read and use theirs.
        await session.rollback()
        return (await session.scalars(stmt)).one()
