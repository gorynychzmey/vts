"""Rewrite untouched vendor prompt copies. Run once per deploy (vts-kujy)."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vts.core.config import get_settings
from vts.services.system_prompt import refresh_untouched_system_prompts


async def _main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await refresh_untouched_system_prompts(session, settings.prompts_dir)
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
