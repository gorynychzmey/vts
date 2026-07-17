import pytest
from sqlalchemy import text

from _db import make_test_engine, ensure_pgvector


@pytest.mark.asyncio
async def test_vector_extension_present():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        row = await conn.execute(
            text("SELECT installed_version FROM pg_available_extensions WHERE name='vector'")
        )
        assert row.scalar() is not None
    await engine.dispose()
