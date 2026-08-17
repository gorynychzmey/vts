"""Finalizing a multi-file set (vts-vm0).

Finalize is where the bytes first exist, so it is where ordering is resolved by
probing and where video compatibility is enforced.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from vts.core.config import get_settings
from vts.services.upload_session import UploadSession

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


@pytest.mark.asyncio
async def test_finalize_failure_after_rename_leaves_session_retryable(client, tmp_path, monkeypatch):
    """A crash between finalize_multi and the Task row commit must not strand
    the upload (vts-vm0 review finding 1).

    finalize_multi renames every staging .part into its final name — the
    point of no return for the bytes on disk — but a Task row does not exist
    yet: probing, verify_probes and the concat-order rename are all still
    ahead. If the process dies (or, as simulated here, probing raises) in
    that window, the upload.json sidecar must still be present, because:
      - UploadSession.load(...) is how a retried finalize call finds the
        session again (without it, _load_owned_session 404s the retry)
      - find_abandoned_sessions() only ever reclaims a directory that is
        MISSING upload.json — a directory that still has it is treated as
        live, in-progress work and is never garbage collected

    So this test asserts the sidecar survives a mid-finalize failure, which
    is exactly what makes the upload recoverable rather than orphaned.
    """
    monkeypatch.setattr(
        "vts.api.routers.uploads.probe_media",
        lambda path: (_ for _ in ()).throw(RuntimeError("boom: simulated probe crash")),
    )

    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    b = _make_audio(tmp_path / "b.m4a", 1, 880)
    upload_id = await _upload_set(client, [("a.m4a", a), ("b.m4a", b)])

    r = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r.status_code == 422
    assert "boom" in r.json()["detail"]

    # The session must still be loadable — this is the retry property.
    settings = get_settings()
    meta = UploadSession.load(settings.artifacts_root, "tester", uuid.UUID(upload_id))
    assert meta is not None, (
        "upload.json was removed before the Task row existed — a retry of "
        "this upload_id would 404, and find_abandoned_sessions() would treat "
        "the directory as GC-eligible: the upload is stranded."
    )
    assert meta.get("files"), "multi-file session metadata must round-trip its files list"


@pytest.mark.asyncio
async def test_finalize_retry_after_crash_past_rename_succeeds(client, tmp_path, monkeypatch):
    """Blocker 2 (vts-vm0 final review): a crash AFTER finalize_multi has
    renamed every `.part` to its final name must still let a retried
    finalize call succeed and create the task.

    vts-pe5 was closed on the belief that the session "stays retryable" —
    but the completeness pre-check in uploads_finalize measures
    UploadSession.part_path_for(...), which points at the `.part` name.
    Once finalize_multi has renamed the parts away, that check finds 0 bytes
    received forever, and finalize_multi's own missing-`.part` guard also
    fires on retry (the finals are there under their real names, not
    `.part`). Both add up to a PERMANENT 409, not a recoverable one: this
    test actually retries (unlike the old
    test_finalize_failure_after_rename_leaves_session_retryable, which only
    checked UploadSession.load(...) is not None and never posted a second
    finalize) and must observe the retry succeed.
    """
    from vts.services.media import probe_media as real_probe_media

    call_count = {"n": 0}

    def _probe_boom_once(path):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom: simulated crash after finalize_multi renamed the parts")
        return real_probe_media(path)

    monkeypatch.setattr("vts.api.routers.uploads.probe_media", _probe_boom_once)

    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    b = _make_audio(tmp_path / "b.m4a", 1, 880)
    upload_id = await _upload_set(client, [("a.m4a", a), ("b.m4a", b)])

    r1 = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r1.status_code == 422
    assert "boom" in r1.json()["detail"]

    # The parts really are renamed away from `.part` at this point — this is
    # exactly the state that used to make retries fail forever.
    settings = get_settings()
    meta = UploadSession.load(settings.artifacts_root, "tester", uuid.UUID(upload_id))
    assert meta is not None

    r2 = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["source_url"].startswith("file://")

    # The session must be fully consumed now — a task exists and the sidecar
    # is gone, exactly like the non-crash happy path.
    meta_after = UploadSession.load(settings.artifacts_root, "tester", uuid.UUID(upload_id))
    assert meta_after is None, "upload.json must be removed once the task row is committed"


@pytest.mark.asyncio
async def test_finalize_retry_after_crash_fully_reordered_succeeds(client, tmp_path, monkeypatch):
    """Blocker 2, third window: a crash AFTER the concat-order reorder has
    fully completed but BEFORE the Task row commits. By this point every
    part sits at its final concat-order name — which, for a genuine
    permutation (not the identity), is NOT the same name finalize_multi or
    the naive completeness pre-check know how to find (they only know the
    SELECTION-index name). A retried finalize must still succeed.
    """
    from vts.db.repo import Repo

    real_create_task = Repo.create_task
    call_count = {"n": 0}

    async def _create_task_boom_once(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom: simulated crash after reorder, before Task commit")
        return await real_create_task(self, *args, **kwargs)

    monkeypatch.setattr(Repo, "create_task", _create_task_boom_once)

    # Select b-then-a with no creation_time metadata, so resolve_order's
    # filename fallback swaps them (a ends up at position 0, b at position
    # 1) — a genuine, non-identity permutation (see the sibling test above
    # for why this matters).
    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    b = _make_audio(tmp_path / "b.m4a", 1, 880)
    upload_id = await _upload_set(client, [("b.m4a", b), ("a.m4a", a)])

    with pytest.raises(RuntimeError, match="boom"):
        await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)

    settings = get_settings()
    meta = UploadSession.load(settings.artifacts_root, "tester", uuid.UUID(upload_id))
    assert meta is not None, "session must still be retryable after this crash point"

    r2 = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert [f["name"] for f in body["options"]["source_files"]] == ["a.m4a", "b.m4a"]

    real_media_dir = Path(body["media_path"]).parent
    names = sorted(
        p.name for p in real_media_dir.glob("audio.original.*")
        if not p.name.endswith(".probe.json")
    )
    assert names == ["audio.original.000.m4a", "audio.original.001.m4a"]

    meta_after = UploadSession.load(settings.artifacts_root, "tester", uuid.UUID(upload_id))
    assert meta_after is None, "upload.json must be removed once the task row is committed"


@pytest.mark.asyncio
async def test_finalize_retry_after_crash_between_rename_passes_succeeds(client, tmp_path, monkeypatch):
    """Blocker 2, second window: a crash BETWEEN the two rename passes of
    _rename_to_concat_order leaves `ordered.NNN.*` files that nothing in the
    codebase recognises. A retried finalize must recover them and still
    succeed.
    """
    real_rename = Path.rename

    def _rename_boom_before_pass_two(self, target):
        # _rename_to_concat_order's first pass renames
        # audio.original.NNN.* -> ordered.NNN.*; its second pass renames
        # ordered.NNN.* -> audio.original.NNN.* (new order). Let every
        # first-pass rename through (target starts with "ordered."), then
        # blow up on the very first second-pass rename (source is
        # "ordered.*", target is not) so at least one — but not all —
        # `ordered.*` file is left behind for the retry to recover.
        if str(self.name).startswith("ordered.") and not str(target.name).startswith("ordered."):
            raise RuntimeError("boom: simulated crash between rename passes")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _rename_boom_before_pass_two)

    # Select b-then-a (selection index 0=b, 1=a) with no creation_time
    # metadata, so resolve_order falls back to natural filename sort: a
    # (selection index 1) becomes position 0, b (selection index 0) becomes
    # position 1. This is a genuine swap — unlike an identity permutation,
    # neither entry's selection-index final name already sits at its
    # concat-order final name, so the reorder rename actually has work to
    # do and can be caught mid-flight.
    a = _make_audio(tmp_path / "a.m4a", 1, 440)
    b = _make_audio(tmp_path / "b.m4a", 1, 880)
    upload_id = await _upload_set(client, [("b.m4a", b), ("a.m4a", a)])

    # Today this rename step is unwrapped, so the simulated crash propagates
    # as a raw exception rather than a clean HTTP error response — that is
    # itself part of the bug this test documents. Accept either shape: the
    # fix's job is to make the RETRY succeed and recover the ordered.*
    # leftovers, not to police the first call's exact error transport.
    try:
        r1 = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
        assert r1.status_code >= 400
    except RuntimeError as exc:
        assert "boom" in str(exc)

    settings = get_settings()

    # Retry with the crash removed.
    monkeypatch.setattr(Path, "rename", real_rename)
    r2 = await client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    real_media_dir = Path(body["media_path"]).parent
    names = sorted(
        p.name for p in real_media_dir.glob("audio.original.*")
        if not p.name.endswith(".probe.json")
    )
    assert names == ["audio.original.000.m4a", "audio.original.001.m4a"]
    assert not list(real_media_dir.glob("ordered.*")), "ordered.* leftovers must be recovered"
    assert not list(real_media_dir.glob("*.part"))


@pytest.mark.asyncio
async def test_concurrent_finalize_creates_exactly_one_task(client, tmp_path, monkeypatch):
    """Two simultaneous finalize calls on one session must not race (vts-hh1).

    Nothing serialized `uploads_finalize` by upload_id, so two concurrent
    POSTs could both pass the completeness check and both run
    `_rename_to_concat_order`, whose check-then-act (`if exists(): rename()`)
    is not safe to interleave. The DB primary key (Task.id == upload_id) was
    the only backstop, so the loser surfaced as an IntegrityError rather
    than a clean response.

    The race window is real but narrow, so it is widened deterministically
    here: `probe_media` yields to the event loop, which lets the second
    request run the whole unprotected prefix while the first is suspended
    mid-probe. Without a lock this reliably reproduces; with one the second
    request simply waits.

    Asserted: exactly one Task row exists, both responses agree on its id,
    and neither reports a primary-key crash.
    """
    import asyncio

    from vts.api.routers.uploads import probe_media as real_probe

    def _probe_with_yield(path):
        # probe_media is called via asyncio.to_thread in a lambda; making it
        # block briefly is enough to overlap the two requests in real time.
        import time

        time.sleep(0.05)
        return real_probe(path)

    monkeypatch.setattr("vts.api.routers.uploads.probe_media", _probe_with_yield)

    a = _make_audio(tmp_path / "a.m4a", 1, 440, creation="2026-08-01T10:00:00.000000Z")
    b = _make_audio(tmp_path / "b.m4a", 1, 880, creation="2026-08-01T10:05:00.000000Z")
    upload_id = await _upload_set(client, [("b.m4a", b), ("a.m4a", a)])

    r1, r2 = await asyncio.gather(
        client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS),
        client.post(f"/api/uploads/{upload_id}/finalize", headers=_HEADERS),
        return_exceptions=True,
    )

    for r in (r1, r2):
        assert not isinstance(r, BaseException), f"finalize raised instead of responding: {r!r}"

    ok = [r for r in (r1, r2) if r.status_code == 200]
    assert ok, f"both finalize calls failed: {r1.status_code} {r1.text} / {r2.status_code} {r2.text}"

    # Whatever the losing request does, it must not be a primary-key crash
    # leaking through as a 500.
    for r in (r1, r2):
        assert r.status_code < 500, f"finalize crashed with {r.status_code}: {r.text}"

    # Both winners must describe the SAME task — one upload, one task.
    ids = {r.json()["id"] for r in ok}
    assert len(ids) == 1, f"concurrent finalize produced multiple tasks: {ids}"

    media_dir = Path(ok[0].json()["media_path"]).parent
    names = sorted(
        p.name for p in media_dir.glob("audio.original.*")
        if not p.name.endswith(".probe.json")
    )
    assert names == ["audio.original.000.m4a", "audio.original.001.m4a"], (
        f"concurrent renames corrupted the media set: {names}"
    )
    assert not list(media_dir.glob("ordered.*"))
    assert not list(media_dir.glob("*.part"))
