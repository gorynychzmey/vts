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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await refresh_untouched_system_prompts(session, settings.prompts_dir)
        await session.commit()
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

    Same ruling, and the same reason, as the delivery plugin loader (vts-j8gz).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_main())
    except Exception:
        logger.exception("system prompt refresh failed; users keep the previous text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
