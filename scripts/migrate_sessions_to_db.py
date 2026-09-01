#!/usr/bin/env python3
"""Move live browser sessions from Redis into the database (vts-akf8).

One-shot. Run it against production once, after `alembic upgrade head` has
created `user_sessions` and BEFORE deploying the code that reads sessions from
the database — in that window the old code is still serving from Redis, so
nobody is logged out, and the sids are still readable there in plain text.
Afterwards they exist only as SHA-256 hashes and cannot be reconstructed: a
session missed here can only be replaced by its owner logging in again.

Safe to re-run. Rows are matched on the sid hash, so a second pass over the
same keys inserts nothing.

Sessions whose email has no user row are skipped rather than invented: the row
carries a foreign key, and an ownerless session should not exist.

    python scripts/migrate_sessions_to_db.py            # report only
    python scripts/migrate_sessions_to_db.py --commit   # actually write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta

import sqlalchemy as sa
from redis.asyncio import Redis

from vts.core.config import get_settings
from vts.db.models import User, UserSession, utcnow
from vts.db.session import SessionLocal
from vts.services.session_store import hash_sid


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="write the rows; without it the script only reports what it would do",
    )
    args = parser.parse_args()

    settings = get_settings()
    prefix = f"{settings.redis_prefix}session:"
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    moved = skipped_no_user = skipped_present = skipped_bad = 0
    try:
        async with SessionLocal() as db:
            async for key in redis.scan_iter(match=f"{prefix}*", count=500):
                sid = key[len(prefix):]
                raw = await redis.get(key)
                ttl = await redis.ttl(key)
                if raw is None:
                    continue
                try:
                    data = json.loads(raw)
                    email = str(data["email"])
                    issued_at = int(data["issued_at"])
                except (ValueError, KeyError, TypeError):
                    print(f"skip {sid[:8]}…: unreadable record", file=sys.stderr)
                    skipped_bad += 1
                    continue

                sid_hash = hash_sid(sid)
                exists = await db.scalar(
                    sa.select(UserSession.id).where(UserSession.sid_hash == sid_hash)
                )
                if exists is not None:
                    skipped_present += 1
                    continue

                user_id = await db.scalar(
                    sa.select(User.id).where(User.username == email)
                )
                if user_id is None:
                    print(f"skip {sid[:8]}… ({email}): no user row", file=sys.stderr)
                    skipped_no_user += 1
                    continue

                # A key with no TTL (-1) or already gone (-2) should not get a
                # longer life than it had; fall back to the configured max age
                # only for the former, and skip the latter.
                if ttl is None or ttl == -2:
                    continue
                seconds = ttl if ttl > 0 else settings.session_max_age_days * 86_400

                if args.commit:
                    db.add(
                        UserSession(
                            user_id=user_id,
                            email=email,
                            sid_hash=sid_hash,
                            issued_at=issued_at,
                            expires_at=utcnow() + timedelta(seconds=seconds),
                        )
                    )
                moved += 1

            if args.commit:
                await db.commit()
    finally:
        await redis.aclose()

    verb = "moved" if args.commit else "would move"
    print(
        f"{verb}: {moved}, already in the database: {skipped_present}, "
        f"no user row: {skipped_no_user}, unreadable: {skipped_bad}"
    )
    if not args.commit:
        print("(dry run — pass --commit to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
