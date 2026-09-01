"""Server-side session record backed by Postgres.

The cookie carries only an opaque `sid` (128-bit random). The
`{sid -> email}` mapping lives in the database with an explicit
`expires_at`. /auth/logout deletes the record so a captured cookie cannot
be replayed afterwards, closing the durable half of OAuth audit Finding 1
(vts-pa9).

Storage moved here from Redis in vts-akf8. Redis is a cache and a bus in
this deployment, not durable storage: production runs it without AOF and
shares the instance with other tenants, so a hard restart silently logged
every user out. The sid is stored as a SHA-256 hash, the way api_tokens
stores token_hash — a database row is dumped, backed up and kept, unlike a
Redis key that expired on its own.

Backwards compatibility: cookies issued before vts-pa9 carry `email`
directly; the resolver falls back to that. The fallback path is safe
because vts-jo2's per-request allow-list re-check still applies.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import User, UserSession, utcnow


def hash_sid(sid: str) -> str:
    """SHA-256 of the sid, hex. Plain hashing, no salt or KDF on purpose: the
    sid is 128 bits of `secrets.token_hex` entropy, so there is no guessable
    input space for a rainbow table or a brute-force pass to work against —
    what a KDF buys for low-entropy passwords it cannot add here."""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionRecord:
    email: str
    issued_at: int


async def create(
    session: AsyncSession, *, email: str, ttl_seconds: int, issued_at: int
) -> str | None:
    """Generate a new sid, persist the record, return the sid.

    Returns None when no user owns that email: the row carries a foreign key,
    and callers create the user first (auth_routes does, right before this).
    Inventing an ownerless session instead would be worse than refusing one.
    """
    user_id = await session.scalar(sa.select(User.id).where(User.username == email))
    if user_id is None:
        return None
    sid = secrets.token_hex(16)
    session.add(
        UserSession(
            user_id=user_id,
            email=email,
            sid_hash=hash_sid(sid),
            issued_at=issued_at,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
    )
    await session.flush()
    return sid


async def lookup(session: AsyncSession, sid: str) -> SessionRecord | None:
    """Return the SessionRecord for sid, or None if missing or expired.

    The expiry check is explicit here because a row, unlike a Redis key, does
    not remove itself when its time is up — `purge_expired` only reclaims the
    space afterwards, and must never be what decides whether a session is live.
    """
    row = await session.scalar(
        sa.select(UserSession).where(
            UserSession.sid_hash == hash_sid(sid),
            UserSession.expires_at > utcnow(),
        )
    )
    if row is None:
        return None
    return SessionRecord(email=row.email, issued_at=row.issued_at)


async def delete(session: AsyncSession, sid: str) -> None:
    """Remove the session record; safe to call on an already-missing sid."""
    await session.execute(
        sa.delete(UserSession).where(UserSession.sid_hash == hash_sid(sid))
    )


async def purge_expired(session: AsyncSession) -> int:
    """Drop rows whose expiry has passed; returns how many went.

    Redis reclaimed expired keys itself. Here nothing does, so without a sweep
    the table keeps every session ever issued for the life of the deployment.
    """
    result = await session.execute(
        sa.delete(UserSession).where(UserSession.expires_at <= utcnow())
    )
    return int(result.rowcount or 0)
