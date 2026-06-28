from __future__ import annotations

import uuid
import pytest

pytestmark = pytest.mark.asyncio

_INIT = {"filename": "clip.mp4", "total_size": 6, "transcript": True}


class _FakeRedis:
    """Minimal async Redis stub: enough for RedisBus.notify_queued / publish
    and the queue-position get/setex cache."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def publish(self, channel, message) -> int:
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


@pytest.fixture(autouse=True)
def _tmp_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    # _isolate_settings_per_test (autouse in conftest) clears the settings
    # cache around each test, so the env var is picked up by create_app().
    yield


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    """Attach a FakeRedis to app.state so get_redis() works without a real Redis."""
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


async def _init(client):
    r = await client.post("/api/uploads/init", json=_INIT)
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


async def test_config_returns_thresholds(client):
    body = (await client.get("/api/uploads/config")).json()
    assert body["chunked_threshold_bytes"] == 52_428_800
    assert body["chunk_bytes"] == 8_388_608
    assert body["max_upload_bytes"] == 2_147_483_648


async def test_happy_path_creates_queued_task(client):
    uid = await _init(client)
    r1 = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"abc")
    assert r1.status_code == 200 and r1.json()["received"] == 3
    r2 = await client.patch(f"/api/uploads/{uid}?offset=3", content=b"def")
    assert r2.json()["received"] == 6
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text
    task = fin.json()
    assert task["status"] == "queued"
    # task is listed now
    tasks = (await client.get("/api/tasks")).json()
    assert any(t["id"] == task["id"] for t in tasks)


async def test_offset_endpoint_supports_resume(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"ab")
    off = (await client.get(f"/api/uploads/{uid}/offset")).json()
    assert off == {"received": 2, "total_size": 6}
    await client.patch(f"/api/uploads/{uid}?offset=2", content=b"cdef")
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200


async def test_wrong_offset_conflicts(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"abc")
    bad = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"x")
    assert bad.status_code == 409


async def test_overflow_rejected(client):
    uid = await _init(client)
    over = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"toolong!")
    assert over.status_code == 413


async def test_init_rejects_bad_suffix(client):
    r = await client.post("/api/uploads/init", json={"filename": "x.txt", "total_size": 5})
    assert r.status_code == 422


async def test_init_rejects_oversize(client):
    r = await client.post("/api/uploads/init",
                          json={"filename": "x.mp4", "total_size": 2_147_483_649})
    assert r.status_code == 413


async def test_finalize_incomplete_conflicts(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"ab")
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 409


async def test_unknown_upload_is_404(client):
    r = await client.get(f"/api/uploads/{uuid.uuid4()}/offset")
    assert r.status_code == 404


async def test_foreign_session_is_404(client):
    """Owner-isolation: a session that doesn't belong to the authenticated user returns 404.

    Two isolation layers are exercised:

    1. Filesystem-scoping by username: a session planted under a *different*
       username ("someone-else") is invisible to the "tester" client because
       UploadSession.load() scopes its lookup to user.username.  The load()
       call returns None -> 404.

    2. user_id mismatch: a session planted under the *same* username ("tester")
       but with a *different* user_id is caught by the second guard in
       _load_owned_session: `meta.get("user_id") != user.id` -> 404.
       (This branch is reachable because load() finds the meta file — same
       username means same on-disk path prefix — but the user_id differs.)
    """
    from vts.core.config import get_settings
    from vts.services.upload_session import UploadSession

    settings = get_settings()

    # --- Case 1: session under a completely different username ---
    foreign_id = uuid.uuid4()
    UploadSession.init(
        settings.artifacts_root,
        "someone-else",
        user_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        upload_id=foreign_id,
        suffix=".mp4",
        total_size=10,
        options={},
        display_name=None,
        filename="x.mp4",
        created_at="t",
    )
    # tester's client loads via tester's username path -> load() returns None -> 404
    r = await client.get(f"/api/uploads/{foreign_id}/offset")
    assert r.status_code == 404
    fin = await client.post(f"/api/uploads/{foreign_id}/finalize")
    assert fin.status_code == 404

    # --- Case 2: session under tester's username but a different user_id ---
    # load() succeeds (same path), but user_id check fails -> 404
    hijack_id = uuid.uuid4()
    UploadSession.init(
        settings.artifacts_root,
        "tester",
        user_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        upload_id=hijack_id,
        suffix=".mp4",
        total_size=10,
        options={},
        display_name=None,
        filename="x.mp4",
        created_at="t",
    )
    r2 = await client.get(f"/api/uploads/{hijack_id}/offset")
    assert r2.status_code == 404
    fin2 = await client.post(f"/api/uploads/{hijack_id}/finalize")
    assert fin2.status_code == 404
