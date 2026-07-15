from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from vts.services.upload_session import (
    UploadSession,
    delete_abandoned_sessions,
    find_abandoned_sessions,
    purge_abandoned_sessions,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _session(root, username: str, *, age_hours: float, suffix: str = ".mp4") -> tuple[uuid.UUID, object]:
    uid = uuid.uuid4()
    created = datetime.now(tz=timezone.utc) - timedelta(hours=age_hours)
    d = UploadSession.init(
        root, username, user_id="u1", upload_id=uid, suffix=suffix,
        total_size=100, options={"transcript": True}, display_name=None,
        filename=f"clip{suffix}", created_at=_iso(created),
    )
    return uid, d


def test_purges_only_sessions_older_than_ttl(tmp_path):
    old_id, old_dir = _session(tmp_path, "tester", age_hours=48)
    fresh_id, fresh_dir = _session(tmp_path, "tester", age_hours=1)

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=24 * 3600, has_task=lambda _tid: False)

    assert removed == [old_id]
    assert not old_dir.exists(), "abandoned session older than the TTL must be removed"
    assert fresh_dir.exists(), "a session still inside its TTL is an upload in progress"


def test_never_touches_a_finalized_task(tmp_path):
    # finalize() unlinks upload.json and renames .part -> the real media file.
    uid, d = _session(tmp_path, "tester", age_hours=99)
    part = d / "media" / "audio.original.mp4.part"
    UploadSession.finalize(part, ".mp4", d / "upload.json")

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda _tid: False)

    assert removed == []
    assert d.exists(), "a directory without upload.json is not an abandoned session"
    assert (d / "media" / "audio.original.mp4").exists()


def test_never_touches_a_dir_that_has_a_task_row(tmp_path):
    # Belt-and-braces: upload.json and a Task row are mutually exclusive by
    # construction (finalize unlinks the sidecar before create_task), but if a
    # row somehow exists the directory is live data — never delete it.
    uid, d = _session(tmp_path, "tester", age_hours=99)

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda tid: tid == uid)

    assert removed == []
    assert d.exists()


def test_unparseable_or_missing_created_at_is_left_alone(tmp_path):
    uid, d = _session(tmp_path, "tester", age_hours=99)
    meta = json.loads((d / "upload.json").read_text())
    meta["created_at"] = "not-a-date"
    (d / "upload.json").write_text(json.dumps(meta))

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda _tid: False)

    assert removed == [], "an unreadable age must fail closed, not delete"
    assert d.exists()


def test_corrupt_sidecar_is_left_alone(tmp_path):
    uid, d = _session(tmp_path, "tester", age_hours=99)
    (d / "upload.json").write_text("{ not json")

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda _tid: False)

    assert removed == []
    assert d.exists()


def test_ignores_directories_that_are_not_task_dirs(tmp_path):
    # A stray non-UUID directory must never be walked into or removed.
    stray = tmp_path / "abc123def" / "not-a-uuid"
    stray.mkdir(parents=True)
    (stray / "upload.json").write_text(json.dumps({"created_at": _iso(datetime(2000, 1, 1, tzinfo=timezone.utc))}))

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda _tid: False)

    assert removed == []
    assert stray.exists()


def test_missing_artifacts_root_is_not_an_error(tmp_path):
    assert purge_abandoned_sessions(tmp_path / "nope", ttl_seconds=1, has_task=lambda _tid: False) == []


def test_finalize_between_scan_and_delete_is_not_removed(tmp_path):
    """The dangerous window: a scan lists a session, the user finalizes it, and
    the delete pass then runs. The directory is now a real task's media and must
    survive — the sweep re-checks the sidecar before unlinking anything."""
    uid, d = _session(tmp_path, "tester", age_hours=99)
    candidates = find_abandoned_sessions(tmp_path, ttl_seconds=1)
    assert uid in candidates, "precondition: the session is a candidate"

    # ...user finalizes right here...
    UploadSession.finalize(d / "media" / "audio.original.mp4.part", ".mp4", d / "upload.json")

    removed = delete_abandoned_sessions(candidates, has_task=lambda _tid: False)

    assert removed == []
    assert d.exists() and (d / "media" / "audio.original.mp4").exists()


def test_purges_across_users(tmp_path):
    a_id, a_dir = _session(tmp_path, "alice", age_hours=48)
    b_id, b_dir = _session(tmp_path, "bob", age_hours=48)

    removed = purge_abandoned_sessions(tmp_path, ttl_seconds=1, has_task=lambda _tid: False)

    assert sorted(map(str, removed)) == sorted([str(a_id), str(b_id)])
    assert not a_dir.exists() and not b_dir.exists()
