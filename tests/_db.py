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
    """CREATE EXTENSION vector before create_all — Vector columns need it, and
    tests build the schema with create_all rather than running migrations."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
