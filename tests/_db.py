"""Shared test database (real Postgres) helpers.

Tests run against a real Postgres instance — production uses Postgres+asyncpg,
and SQLite was only ever a test shortcut. The URL comes from
VTS_TEST_DATABASE_URL (set by CI runners); the default targets a local dev
Postgres. Unlike SQLite :memory:, Postgres does not give per-test isolation
for free, so fixtures drop+recreate the schema around each test.
"""
from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "VTS_TEST_DATABASE_URL",
    "postgresql+asyncpg://vts:vts@localhost:5432/vts_test",
)


def make_test_engine():
    """Create an async engine pointed at the test Postgres URL."""
    return create_async_engine(TEST_DATABASE_URL, echo=False)


async def ensure_pgvector(engine) -> None:
    """Verify the vector extension is present before create_all — Vector columns
    need it, and tests build the schema with create_all rather than migrations.

    This checks instead of creating. The test role is deliberately not a
    superuser (matching production), so it *cannot* create the extension; the
    test database provisions it as superuser at init time. Creating it here was
    what let the pgvector privilege bug pass locally and in CI while being
    impossible in prod (vts-e1p).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        if result.first() is None:
            raise RuntimeError(
                "The 'vector' extension is missing from the test database.\n"
                "It must be created by a superuser before running tests:\n"
                "  psql -U postgres -d vts_test -c 'CREATE EXTENSION vector'\n"
                "docker-compose provisions this automatically via "
                "scripts/pg-init-app-role.sh."
            )
