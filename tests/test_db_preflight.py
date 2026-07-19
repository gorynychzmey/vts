"""Preflight checks that run before migrations.

Regression cover for the 2026-07-19 production outage (vts-e1p): migration
0014 ran `CREATE EXTENSION vector` as the unprivileged application role,
failed with InsufficientPrivilegeError, and turned every start into a
crash loop behind a 502. The preflight turns that into one actionable
message naming the extension, the role, and the command to fix it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from _db import make_test_engine
from vts.db.preflight import (
    PreflightError,
    check_required_extensions,
    missing_extensions,
)


@pytest.mark.asyncio
async def test_missing_extensions_reports_absent_extension():
    """An extension that is not installed is reported as missing."""
    engine = make_test_engine()
    try:
        missing = await missing_extensions(engine, ["definitely_not_an_extension"])
    finally:
        await engine.dispose()
    assert missing == ["definitely_not_an_extension"]


@pytest.mark.asyncio
async def test_missing_extensions_ignores_installed_extension():
    """plpgsql ships enabled in every Postgres database, so it is never missing."""
    engine = make_test_engine()
    try:
        missing = await missing_extensions(engine, ["plpgsql"])
    finally:
        await engine.dispose()
    assert missing == []


@pytest.mark.asyncio
async def test_check_passes_when_extension_present():
    """The happy path must not raise — vector is installed by test setup."""
    engine = make_test_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await check_required_extensions(engine, ["vector"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_raises_actionable_error_when_missing():
    """The failure names the extension and the exact SQL to run as superuser.

    This is the message an operator sees instead of a raw asyncpg traceback.
    """
    engine = make_test_engine()
    try:
        with pytest.raises(PreflightError) as excinfo:
            await check_required_extensions(engine, ["definitely_not_an_extension"])
    finally:
        await engine.dispose()

    message = str(excinfo.value)
    assert "definitely_not_an_extension" in message
    assert "CREATE EXTENSION" in message
    assert "superuser" in message.lower()
