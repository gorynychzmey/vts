"""Server-side session record backed by Redis.

The cookie carries only an opaque `sid` (128-bit random). The
`{sid -> email}` mapping lives in Redis with the cookie's max-age as
TTL. /auth/logout deletes the record so a captured cookie cannot be
replayed afterwards, closing the durable half of OAuth audit Finding 1
(vts-pa9).

Backwards compatibility: cookies issued before vts-pa9 carry `email`
directly; the resolver falls back to that. The fallback path is safe
because vts-jo2's per-request allow-list re-check still applies.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

_SESSION_KEY_PREFIX = "vts:session:"


def _key(sid: str) -> str:
    return f"{_SESSION_KEY_PREFIX}{sid}"


@dataclass(frozen=True)
class SessionRecord:
    email: str
    issued_at: int


async def create(redis: Any, *, email: str, ttl_seconds: int, issued_at: int) -> str:
    """Generate a new sid, persist {sid -> email} with TTL, return sid."""
    sid = secrets.token_hex(16)
    payload = json.dumps({"email": email, "issued_at": issued_at}).encode("utf-8")
    await redis.set(_key(sid), payload, ex=ttl_seconds)
    return sid


async def lookup(redis: Any, sid: str) -> SessionRecord | None:
    """Return the SessionRecord for sid, or None if missing/expired."""
    raw = await redis.get(_key(sid))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    return SessionRecord(email=str(data["email"]), issued_at=int(data["issued_at"]))


async def delete(redis: Any, sid: str) -> None:
    """Remove the session record; safe to call on already-missing sid."""
    await redis.delete(_key(sid))
