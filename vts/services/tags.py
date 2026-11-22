"""Tag names: normalisation and identity (vts-qwcj).

A tag's identity is its `name_key`, the casefolded normalised name, computed
here in Python and never in SQL: Postgres `lower()` depends on the database
locale, `str.casefold()` does not.
"""
from __future__ import annotations

from collections.abc import Iterable

MAX_TAG_NAME_LENGTH = 64


class TagNameError(ValueError):
    """The name is empty or too long once normalised."""


class TagExists(Exception):
    """The user already has a tag with this key."""


def normalize_tag_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise TagNameError("tag name is empty")
    if len(name) > MAX_TAG_NAME_LENGTH:
        raise TagNameError(f"tag name is longer than {MAX_TAG_NAME_LENGTH} characters")
    return name


def tag_key(raw: str) -> str:
    return normalize_tag_name(raw).casefold()


def dedupe_names(names: Iterable[str]) -> list[str]:
    """Normalised names, one per key (the first spelling wins), order kept."""
    seen: dict[str, str] = {}
    for raw in names:
        name = normalize_tag_name(raw)
        seen.setdefault(name.casefold(), name)
    return list(seen.values())


def preset_tag_ids(options: dict | None) -> list[str]:
    """The tag ids a preset's options hold (as strings), tolerating junk."""
    tags = (options or {}).get("tags")
    return [str(t) for t in tags] if isinstance(tags, list) else []


async def keep_own_tags(repo, user_id, options: dict) -> dict:
    """Preset options to store: tag ids that are not the user's existing
    tags (unknown, deleted, another user's, malformed) are dropped. Shared by
    the HTTP and MCP preset writers so both save the same thing."""
    wanted = preset_tag_ids(options)
    own = await repo.own_tag_ids(user_id, wanted)
    return {**options, "tags": list(dict.fromkeys(t for t in wanted if t in own))}


async def visible_preset_options(repo, user_id, rows) -> list[dict]:
    """Options of user preset rows as they are read: only tag ids that are
    still this user's tags (a deleted tag is skipped before the next save
    removes it). One query for all rows."""
    own = await repo.own_tag_ids(user_id, [t for r in rows for t in preset_tag_ids(r.options)])
    return [
        {**(r.options or {}), "tags": [t for t in preset_tag_ids(r.options) if t in own]}
        for r in rows
    ]
