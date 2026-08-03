"""Multi-file upload API (vts-vm0)."""
from __future__ import annotations

import pytest

_HEADERS = {"X-Forwarded-User": "tester"}


@pytest.fixture(autouse=True)
def _tmp_artifacts(monkeypatch, tmp_path):
    """Same convention as tests/test_uploads_api.py: point artifacts_root at
    a per-test tmp dir so UploadSession writes don't hit the real host path.
    _isolate_settings_per_test (autouse in conftest) clears the settings
    cache around each test, so the env var is picked up by create_app()."""
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    yield


def _files(*specs):
    return [{"filename": n, "total_size": s, "last_modified": m} for n, s, m in specs]


@pytest.mark.asyncio
async def test_init_accepts_a_video_set(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 10, 1000), ("b.mp4", 20, 2000)),
    }, headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_id"]
    assert [f["filename"] for f in body["files"]] == ["a.mp4", "b.mp4"]
    assert [f["index"] for f in body["files"]] == [0, 1]


@pytest.mark.asyncio
async def test_init_rejects_mixed_video_and_audio(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("talk.mp4", 10, 1), ("note.mp3", 10, 2)),
    }, headers=_HEADERS)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "talk.mp4" in detail and "note.mp3" in detail


@pytest.mark.asyncio
async def test_init_rejects_too_many_files(client, monkeypatch):
    from vts.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("VTS_UPLOAD_MAX_FILES", "2")
    get_settings.cache_clear()
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 1, 1), ("b.mp4", 1, 2), ("c.mp4", 1, 3)),
    }, headers=_HEADERS)
    get_settings.cache_clear()
    assert r.status_code == 422
    assert "at most 2" in r.json()["detail"]


@pytest.mark.asyncio
async def test_init_rejects_set_over_total_size(client):
    from vts.core.config import get_settings
    limit = get_settings().max_upload_bytes
    r = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", limit, 1), ("b.mp4", limit, 2)),
    }, headers=_HEADERS)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_patch_writes_to_the_indexed_part(client):
    init = await client.post("/api/uploads/init", json={
        "filename": "unused.mp4", "total_size": 0,
        "files": _files(("a.mp4", 3, 1000), ("b.mp4", 4, 2000)),
    }, headers=_HEADERS)
    upload_id = init.json()["upload_id"]

    r = await client.patch(
        f"/api/uploads/{upload_id}?offset=0&index=1", content=b"bbbb", headers=_HEADERS
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] == 4

    # Index 0 is untouched.
    off = await client.get(f"/api/uploads/{upload_id}/offset?index=0", headers=_HEADERS)
    assert off.json()["received"] == 0


@pytest.mark.asyncio
async def test_single_file_init_still_works(client):
    """The existing single-file path must be untouched."""
    r = await client.post("/api/uploads/init", json={
        "filename": "solo.mp4", "total_size": 100,
    }, headers=_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["upload_id"]


@pytest.mark.asyncio
async def test_init_accepts_files_only_no_legacy_fields(client):
    """The real browser client sends only `files` for a multi-file set — it
    has no single filename/total_size to report honestly. This must be
    accepted without requiring placeholder legacy fields (vts-vm0)."""
    r = await client.post("/api/uploads/init", json={
        "files": _files(("a.mp4", 10, 1000), ("b.mp4", 20, 2000)),
    }, headers=_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_id"]
    assert [f["filename"] for f in body["files"]] == ["a.mp4", "b.mp4"]


@pytest.mark.asyncio
async def test_init_rejects_missing_files_and_missing_legacy_fields(client):
    """Neither a file set nor legacy filename/total_size: must still 422,
    not silently accept an empty/ambiguous request."""
    r = await client.post("/api/uploads/init", json={}, headers=_HEADERS)
    assert r.status_code == 422, r.text
