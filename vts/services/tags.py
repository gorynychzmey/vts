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
