"""Pytest fixtures shared across the test suite.

The autouse `_isolate_settings_from_yaml` fixture stops the dev/prod
config.yaml at /opt/vts/config/config.yaml from leaking into Settings
during tests. Without it, tests that monkeypatch VTS_* env vars and
clear the get_settings lru_cache still see whatever the host yaml
declares (yaml > env in Settings(**overrides) precedence), making
tests environment-dependent.

The module-level monkey-patch of `_load_yaml_overrides` runs BEFORE any
test collection — this is essential because `vts/api/main.py:940` has
`app = create_app()` at module scope. Any test that imports anything
from `vts.api.main` triggers that line during collection, which in
turn calls `build_mcp_server()`. If the host yaml carries
`mcp_oauth_enabled=True` without a client_secret, `build_mcp_server`
raises RuntimeError before any fixture has a chance to run.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine

# Patch BEFORE any test imports vts modules. Tests must not see the host
# config.yaml regardless of fixture ordering.
import vts.core.config as _cfg

_cfg._load_yaml_overrides = lambda: {}  # type: ignore[assignment]
_cfg.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings_per_test(monkeypatch):
    """Per-test: re-assert the yaml monkey-patch and clear the lru_cache
    before and after each test so monkeypatch.setenv changes are seen by
    the next Settings()-via-get_settings() call."""
    monkeypatch.setattr("vts.core.config._load_yaml_overrides", lambda: {})
    _cfg.get_settings.cache_clear()
    yield
    _cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Authenticated-client harness for API endpoint tests.
#
# Builds a fresh app via create_app() and overrides two dependencies:
#   * vts.db.session.get_db_session  -> a Postgres-backed AsyncSession
#     (the SAME session/engine is shared with the seed step, so rows
#     written during setup persist within a test).
#   * vts.services.auth.require_user -> a fixed fake AuthenticatedUser whose
#     id matches a seeded users row (FK from prompts.user_id -> users.id).
# Exposes the `client` fixture (httpx.AsyncClient over ASGITransport).
# ---------------------------------------------------------------------------

# Stable id shared between the seeded User row and the fake AuthenticatedUser.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest_asyncio.fixture
async def authed_app():
    """Fresh app instance with DB + auth dependencies overridden.

    Yields (app, sessionmaker). A single Postgres engine backs both the
    dependency override and the seed step within the test. The schema is
    dropped+recreated around the test for isolation (Postgres has no
    per-connection in-memory database like SQLite :memory:).
    """
    from vts.db.base import Base
    from vts.db.models import User
    from vts.db.session import get_db_session
    from vts.services.auth import AuthenticatedUser, require_user

    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # Seed the user row that the fake AuthenticatedUser will act as.
    async with factory() as seed:
        seed.add(User(id=uuid.UUID(_TEST_USER_ID), username="tester"))
        await seed.commit()

    async def _override_get_db_session():
        async with factory() as session:
            yield session

    async def _override_require_user() -> AuthenticatedUser:
        return AuthenticatedUser(
            id=_TEST_USER_ID,
            username="tester",
            requested_by="tester",
            is_admin=False,
            acting_as="tester",
        )

    from vts.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[require_user] = _override_require_user

    try:
        yield app, factory
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(authed_app):
    """httpx.AsyncClient bound to the authed app via ASGITransport."""
    app, _factory = authed_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
