"""Rewrite untouched vendor prompt copies. Run once per deploy (vts-kujy)."""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vts.core.config import get_settings
from vts.services.system_prompt import refresh_untouched_system_prompts

logger = logging.getLogger(__name__)


async def _main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await refresh_untouched_system_prompts(session, settings.prompts_dir)
            await session.commit()
    finally:
        # `main` swallows the exception below, so without this the pool would
        # be dropped rather than closed — an unclean disconnect in the server
        # log on every failed refresh. Same shape as vts.db.preflight.
        await engine.dispose()


def main() -> int:
    """Always exit 0: a stale prompt must never cost us a deploy.

    This runs inside `migrate()` in the entrypoint, under `set -eu`, so a
    non-zero exit here would abort the deploy *after* `alembic upgrade head`
    has already migrated the schema — webapi and worker would never start, and
    in the pod topology the initContainer would never let them. The failure
    this guards against is not symmetric: a refresh that does not happen
    leaves users on the previous prompt text, which the next deploy fixes on
    its own, while a deploy that does not happen is an outage.

    The delivery plugin loader reasons the same way about the same tradeoff
    (vts-j8gz), but keeps a second tier: it exits 1 for an operator-fixable
    misconfiguration. There is no equivalent tier here — `vendor_text` already
    falls back when the file is missing, empty, or unreadable, so no failure
    left in this path is one an operator could act on mid-deploy.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_main())
    except Exception:
        logger.exception("system prompt refresh failed; users keep the previous text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
