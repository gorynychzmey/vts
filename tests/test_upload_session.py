from __future__ import annotations

import json
import uuid

from vts.services.upload_session import UploadSession


def test_init_creates_structure_and_sidecar(tmp_path):
    uid = uuid.uuid4()
    task_dir = UploadSession.init(
        tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mp4",
        total_size=100, options={"transcript": True}, display_name="Clip",
        filename="movie.mp4", created_at="2026-06-28T00:00:00Z",
    )
    part = task_dir / "media" / "audio.original.mp4.part"
    meta = task_dir / "upload.json"
    assert part.exists() and part.stat().st_size == 0
    data = json.loads(meta.read_text())
    assert data["user_id"] == "u1"
    assert data["suffix"] == ".mp4"
    assert data["total_size"] == 100
    assert data["received"] == 0
    assert data["display_name"] == "Clip"
    assert data["filename"] == "movie.mp4"


def test_append_grows_part_and_received(tmp_path):
    uid = uuid.uuid4()
    UploadSession.init(tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mp4",
                       total_size=6, options={}, display_name=None, filename="a.mp4", created_at="t")
    part = UploadSession.part_path(tmp_path, "tester", uid, ".mp4")
    meta = part.parent.parent / "upload.json"
    assert UploadSession.received_bytes(part) == 0
    n = UploadSession.append_chunk(part, meta, b"abc", total_size=6)
    assert n == 3
    n = UploadSession.append_chunk(part, meta, b"def", total_size=6)
    assert n == 6
    assert part.read_bytes() == b"abcdef"
    assert json.loads(meta.read_text())["received"] == 6


def test_finalize_renames_and_clears_sidecar(tmp_path):
    uid = uuid.uuid4()
    UploadSession.init(tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mkv",
                       total_size=3, options={}, display_name=None, filename="b.mkv", created_at="t")
    part = UploadSession.part_path(tmp_path, "tester", uid, ".mkv")
    meta = part.parent.parent / "upload.json"
    UploadSession.append_chunk(part, meta, b"xyz", total_size=3)
    final = UploadSession.finalize(part, ".mkv", meta)
    assert final.name == "audio.original.mkv"
    assert final.read_bytes() == b"xyz"
    assert not part.exists()
    assert not meta.exists()


def test_load_returns_none_when_absent(tmp_path):
    assert UploadSession.load(tmp_path, "tester", uuid.uuid4()) is None
