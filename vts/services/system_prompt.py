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
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import Prompt
from vts.db.repo import Repo
from vts.services.prompt_registry import list_system_prompts
from vts.services.summarizer import load_prompt

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

_SUMMARY_KEY = "summary"

# Every prompt file the service reads. The registry only knows the one that
# users can copy; segment and pack are read straight from the directory by the
# pipeline, and an operator can override any of them.
_PROMPT_FILES = ("global_prompt.md", "segment_prompt.md", "pack_prompt.md")

# The image's own copy, next to the package rather than at a hardcoded /app:
# this file is vts/services/system_prompt.py, so three parents up is the tree
# root that `COPY . /app` produced.
_IMAGE_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
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


@dataclass(frozen=True)
class PromptOverrideReport:
    """Which prompt files the host directory overrides, and which it does not."""

    overridden: list[str] = field(default_factory=list)
    identical: list[str] = field(default_factory=list)
    from_image: list[str] = field(default_factory=list)

    @property
    def has_overrides(self) -> bool:
        return bool(self.overridden)


def describe_prompt_overrides(
    prompts_dir: Path, image_dir: Path | None = None
) -> PromptOverrideReport:
    """Compare the prompts the service will use against the image's defaults.

    Mounting a directory over the image's own `prompts/` is how an operator
    overrides a prompt for the whole service, and it is designed to survive
    releases — the deploy deliberately does not copy prompts onto the host.
    The cost is that a newly shipped vendor prompt then does not reach
    production and nothing says why. This report is that missing signal.

    Returns empty when there is no image directory to compare against: a dev
    checkout reads its prompts straight from the tree, and warning there would
    be noise that teaches operators to ignore the real warning.
    """
    image = _IMAGE_PROMPTS_DIR if image_dir is None else image_dir
    try:
        if not image.is_dir() or image.resolve() == prompts_dir.resolve():
            return PromptOverrideReport()
    except OSError:
        return PromptOverrideReport()

    overridden: list[str] = []
    identical: list[str] = []
    from_image: list[str] = []
    for name in _PROMPT_FILES:
        shipped = image / name
        if not shipped.is_file():
            continue
        local = prompts_dir / name
        try:
            if not local.is_file():
                from_image.append(name)
            elif local.read_text(encoding="utf-8") == shipped.read_text(encoding="utf-8"):
                identical.append(name)
            else:
                overridden.append(name)
        except OSError:
            # Unreadable is not "identical": say it differs rather than
            # claiming the service runs the default when we cannot tell.
            overridden.append(name)
    return PromptOverrideReport(overridden, identical, from_image)


def log_prompt_overrides(prompts_dir: Path) -> PromptOverrideReport:
    """Report at startup whether the service runs the shipped prompts."""
    report = describe_prompt_overrides(prompts_dir)
    if report.has_overrides:
        logger.warning(
            "prompt override active: %s in %s replace(s) the shipped version, "
            "so a newly released wording of these files does NOT reach this "
            "deployment until the override is updated too",
            ", ".join(report.overridden),
            prompts_dir,
        )
    return report
