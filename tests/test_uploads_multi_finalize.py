"""Finalizing a multi-file set (vts-vm0).

Finalize is where the bytes first exist, so it is where ordering is resolved by
probing and where video compatibility is enforced.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_HEADERS = {"X-Forwarded-User": "tester"}


class _FakeRedis:
    """Minimal async Redis stub: enough for RedisBus.notify_queued / publish
    and the queue-position get/setex cache. Same as tests/test_uploads_api.py."""

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
    """Same convention as tests/test_uploads_multi_api.py: point artifacts_root
    at a per-test tmp dir so UploadSession writes don't hit the real host path.
    _isolate_settings_per_test (autouse in conftest) clears the settings
    cache around each test, so the env var is picked up by create_app()."""
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    """Attach a FakeRedis to app.state so get_redis() works without a real Redis."""
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


def _make_audio(path, seconds, freq, creation=None):
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=16000", "-ac", "1"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path.read_bytes()


async def _upload_set(client, files):
    """files: [(filename, bytes)] -> upload_id"""
    init = await client.post("/api/uploads/init", json={
        "filename": "unused" + files[0][0][-4:], "total_size": 0,
        "files": [{"filename": n, "total_size": len(b), "last_modified": None} for n, b in files],
    }, headers=_HEADERS)
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]
    for index, (_, payload) in enumerate(files):
        r = await client.patch(
            f"/api/uploads/{upload_id}?offset=0&index={index}", content=payload, headers=_HEADERS
        )
        assert r.status_code == 200, r.text
    return upload_id


@pytest.mark.asyncio
async def test_finalize_creates_one_task_with_source_files(client, tmp_path):
    a = _make_audio(tmp_path / "a.m4a", 1, 440, creation="2026-08-01T10:00:00.000000Z")
    b = _make_audio(tmp_path / "b.m4a", 1, 880, creation="2026-08-01T10:05:00.000000Z")
    upload_id = await _upload_set(client, [("b.m4a", b), ("a.m4a", a)])

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()

    options = body["options"]
    assert options["source_files_order"] == "creation_time"
    # Selected b-then-a, but a was recorded first — order must be corrected.
    assert [f["name"] for f in options["source_files"]] == ["a.m4a", "b.m4a"]
    assert body["source_url"].startswith("file://")

    # Prove the rename actually landed in concat order on disk, not just in
    # the reported metadata: staging was audio.original.000 = b (selected
    # first), .001 = a. After the permutation-safe rename, .000 must be a
    # (recorded first) and .001 must be b — and nothing must be left behind
    # (a collision in a naive one-pass rename would drop or duplicate a part).
    media_dir = Path(body["media_path"]).parent
    names = sorted(p.name for p in media_dir.glob("audio.original.*") if not p.name.endswith(".probe.json"))
    assert names == ["audio.original.000.m4a", "audio.original.001.m4a"]
    assert not list(media_dir.glob("*.part"))
    assert not list(media_dir.glob("ordered.*"))


@pytest.mark.asyncio
async def test_finalize_rejects_incompatible_video_set(client, tmp_path):
    def _video(path, size):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
            capture_output=True, check=True,
        )
        return path.read_bytes()

    small = _video(tmp_path / "s.mp4", "320x240")
    large = _video(tmp_path / "l.mp4", "640x480")
    upload_id = await _upload_set(client, [("s.mp4", small), ("l.mp4", large)])

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 422
    assert "l.mp4" in r.json()["detail"]


@pytest.mark.asyncio
async def test_finalize_rejects_incomplete_set(client, tmp_path):
    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    init = await client.post("/api/uploads/init", json={
        "filename": "unused.m4a", "total_size": 0,
        "files": [
            {"filename": "a.m4a", "total_size": len(a), "last_modified": None},
            {"filename": "b.m4a", "total_size": 999, "last_modified": None},
        ],
    }, headers=_HEADERS)
    upload_id = init.json()["upload_id"]
    await client.patch(f"/api/uploads/{upload_id}?offset=0&index=0", content=a, headers=_HEADERS)

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 409
    assert "b.m4a" in r.json()["detail"]
