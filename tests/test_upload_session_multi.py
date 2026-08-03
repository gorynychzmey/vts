"""Multi-part chunked upload storage (vts-vm0).

The existing single-file session names its staging file `audio.original<suffix>`
with no index, so N files in one session would overwrite each other. Parts are
indexed instead, and the index is the resolved concat order.
"""
from __future__ import annotations

import json
import uuid

from vts.services.upload_session import UploadSession, part_name


def _files():
    return [
        {"filename": "a.mp4", "suffix": ".mp4", "total_size": 10, "last_modified": 1000},
        {"filename": "b.mp4", "suffix": ".mp4", "total_size": 20, "last_modified": 2000},
    ]


def test_part_name_is_indexed():
    assert part_name(0, ".mp4") == "audio.original.000.mp4"
    assert part_name(12, ".opus") == "audio.original.012.opus"


def test_init_multi_creates_one_part_per_file(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={"transcript": True}, display_name=None,
        created_at="2026-08-03T00:00:00+00:00",
    )
    meta = UploadSession.load(tmp_path, "tester", upload_id)
    assert meta["kind"] == "video"
    assert [f["filename"] for f in meta["files"]] == ["a.mp4", "b.mp4"]
    assert [f["index"] for f in meta["files"]] == [0, 1]
    for index in (0, 1):
        assert UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4").exists()


def test_parts_do_not_collide(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    p0 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 0, ".mp4")
    p1 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")
    assert p0 != p1


def test_append_chunk_at_tracks_per_file_progress(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)
    part = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")

    assert UploadSession.append_chunk_at(part, meta_path, b"12345", 1) == 5

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["files"][1]["received"] == 5
    assert meta["files"][0]["received"] == 0


def test_finalize_multi_renames_all_parts_in_order(tmp_path):
    upload_id = uuid.uuid4()
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=_files(),
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)
    for index, payload in ((0, b"aaa"), (1, b"bbbb")):
        part = UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4")
        UploadSession.append_chunk_at(part, meta_path, payload, index)

    meta = UploadSession.load(tmp_path, "tester", upload_id)
    finals = UploadSession.finalize_multi(tmp_path, "tester", upload_id, meta)

    assert [p.name for p in finals] == ["audio.original.000.mp4", "audio.original.001.mp4"]
    assert all(p.exists() for p in finals)
    assert not any(p.with_suffix(p.suffix + ".part").exists() for p in finals)
    assert not meta_path.exists()
