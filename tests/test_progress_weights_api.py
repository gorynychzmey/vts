from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo
from vts.metrics.step_weights import SEED_STEP_WEIGHTS, SEED_FINAL_SUMMARY_FALLBACK

pytestmark = pytest.mark.asyncio

# The conftest `client` fixture authenticates as a fixed seeded user "tester"
# (id below, is_admin=False) and does NOT honor ?as_user=. That covers the two
# non-impersonation cases. For impersonation we build a dedicated app whose
# require_user override returns an ADMIN acting_as a seeded target user — this
# is the only way to exercise the endpoint's effective-user scoping via HTTP.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000a1"  # matches tests/conftest.py
_TARGET_USER_ID = "00000000-0000-0000-0000-0000000000b2"


async def test_no_row_returns_seed(client):
    # default authed user ("tester") has no user_step_weights row -> seed
    resp = await client.get("/api/progress-weights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["weights"]["transcribe_segments"] == SEED_STEP_WEIGHTS["transcribe_segments"]
    assert body["final_summary_fallback"] == SEED_FINAL_SUMMARY_FALLBACK


async def test_existing_row_returned(authed_app, client):
    # authed_app yields (app, sessionmaker); seed a row for the "tester" user
    _app, factory = authed_app
    async with factory() as session:
        repo = Repo(session)
        await repo.upsert_user_step_weights(
            uuid.UUID(_TEST_USER_ID), {"download": 12.3}, 321.0,
            datetime.now(tz=timezone.utc), {"download": 9},
        )
        await session.commit()
    body = (await client.get("/api/progress-weights")).json()
    assert body["weights"]["download"] == 12.3
    assert body["final_summary_fallback"] == 321.0


@pytest_asyncio.fixture
async def impersonation_client():
    """App+client where require_user returns an ADMIN acting_as a seeded target.

    Mirrors tests/conftest.py:authed_app but overrides the auth identity to an
    admin impersonating _TARGET_USER_ID, and seeds that target user + a weights
    row, so GET /api/progress-weights must return the TARGET's weights.
    """
    from vts.db.session import get_db_session
    from vts.services.auth import AuthenticatedUser, require_user
    from vts.api.main import create_app

    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as seed:
        seed.add(User(id=uuid.UUID(_TARGET_USER_ID), username="target@example.com"))
        await seed.flush()
        repo = Repo(seed)
        await repo.upsert_user_step_weights(
            uuid.UUID(_TARGET_USER_ID), {"download": 7.7}, 200.0,
            datetime.now(tz=timezone.utc), {"download": 9},
        )
        await seed.commit()

    async def _override_get_db_session():
        async with factory() as session:
            yield session

    async def _override_require_user() -> AuthenticatedUser:
        # auth layer would resolve ?as_user= to the target's id; emulate the
        # resolved result: effective user.id IS the target's id.
        return AuthenticatedUser(
            id=_TARGET_USER_ID,
            username="target@example.com",
            requested_by="admin@example.com",
            is_admin=True,
            acting_as="target@example.com",
        )

    app = create_app()
    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[require_user] = _override_require_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_impersonation_returns_target_user_weights(impersonation_client):
    # admin acting_as target -> endpoint scopes by user.id (= target's id)
    body = (await impersonation_client.get("/api/progress-weights")).json()
    assert body["weights"]["download"] == 7.7
    assert body["final_summary_fallback"] == 200.0
