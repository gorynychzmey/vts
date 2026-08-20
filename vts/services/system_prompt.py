"""Per-user copies of the vendor system prompt (vts-kujy).

The vendor's text stays in `prompts/global_prompt.md`; the database holds each
user's copy of it. Keeping the file as the only reference is what makes
restoring free — deleting the row and reading the file again is the whole
mechanism — at the cost that a copy, once made, does not follow later edits to
the file on its own. `refresh_untouched_system_prompts` closes that gap.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import Prompt
from vts.db.repo import Repo
from vts.services.prompt_registry import list_system_prompts
from vts.services.summarizer import load_prompt

logger = logging.getLogger(__name__)

_SUMMARY_KEY = "summary"
_FALLBACK = "Produce a structured knowledge document from the notes."


def vendor_text(prompts_dir: Path) -> str:
    """The vendor's own wording, straight from the file.

    `load_prompt` only falls back when the file is missing. An empty file
    (a truncated write during deploy, say) or an unreadable one (bad
    permissions, a failed mount) must fall back the same way a missing file
    does — restoring later just deletes the row and re-reads this same file,
    so a silently empty or broken copy would stay broken forever.
    """
    spec = next((p for p in list_system_prompts() if p.key == _SUMMARY_KEY), None)
    file = spec.file if spec is not None else "global_prompt.md"
    try:
        text = load_prompt(prompts_dir, file, _FALLBACK)
    except OSError:
        return _FALLBACK
    return text if text.strip() else _FALLBACK


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
        # A nested transaction (SAVEPOINT) scopes the rollback below to just
        # this INSERT. A bare `session.rollback()` would roll back the whole
        # outer transaction, discarding any work the caller already staged in
        # this session before calling us (a worker task's pending status/
        # progress edits, for instance) — exactly the scenario the partial
        # unique index is meant to make safe.
        async with session.begin_nested():
            created = await Repo(session).create_prompt(
                user_id, vendor_name(), vendor_text(prompts_dir), is_system=True
            )
        return created
    except IntegrityError:
        # The partial unique index rejected us: another caller created the copy
        # between our SELECT and INSERT. That is the race resolving correctly —
        # re-read and use theirs.
        return (await session.scalars(stmt)).one()


async def refresh_untouched_system_prompts(
    session: AsyncSession, prompts_dir: Path
) -> tuple[int, int]:
    """Rewrite every vendor copy the user has not edited. Returns (refreshed, skipped).

    `updated_at IS NULL` is the whole test: it records that the user has never
    edited this copy, and it keeps meaning that across any number of releases.
    The refresh does **not** stamp `updated_at` — rewriting a copy is not a
    user edit, and stamping it would make the copy immune to the next release.
    """
    text = vendor_text(prompts_dir)

    skipped = len(
        (
            await session.scalars(
                sa.select(Prompt.id).where(Prompt.is_system, Prompt.updated_at.is_not(None))
            )
        ).all()
    )
    result = await session.execute(
        sa.update(Prompt)
        .where(Prompt.is_system, Prompt.updated_at.is_(None))
        .values(system_prompt=text)
    )
    refreshed = int(result.rowcount or 0)
    logger.info(
        "system prompt refresh: %d untouched copies rewritten, %d left as edited",
        refreshed,
        skipped,
    )
    return refreshed, skipped
