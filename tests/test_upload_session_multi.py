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
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)
    p0 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 0, ".mp4")
    p1 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")

    # Verify paths are distinct and files exist
    assert p0 != p1
    assert p0.exists()
    assert p1.exists()

    # Write distinct content to each part
    UploadSession.append_chunk_at(p0, meta_path, b"part0_content", 0)
    UploadSession.append_chunk_at(p1, meta_path, b"part1_content", 1)

    # Verify distinct content is in each file
    assert p0.read_bytes() == b"part0_content"
    assert p1.read_bytes() == b"part1_content"


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
    payloads = {0: b"aaa", 1: b"bbbb"}
    for index, payload in payloads.items():
        part = UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4")
        UploadSession.append_chunk_at(part, meta_path, payload, index)

    meta = UploadSession.load(tmp_path, "tester", upload_id)
    finals = UploadSession.finalize_multi(tmp_path, "tester", upload_id, meta)

    # Verify names are in index order
    assert [p.name for p in finals] == ["audio.original.000.mp4", "audio.original.001.mp4"]

    # Verify all final files exist
    assert all(p.exists() for p in finals)

    # Verify content integrity: each file contains the bytes written to that index
    for index, expected_content in payloads.items():
        actual_content = finals[index].read_bytes()
        assert actual_content == expected_content, (
            f"Content mismatch at index {index}: expected {expected_content!r}, "
            f"got {actual_content!r}"
        )

    # Verify staging files are gone
    assert not any(p.with_suffix(p.suffix + ".part").exists() for p in finals)

    # Verify metadata is deleted
    assert not meta_path.exists()


def test_finalize_multi_fails_early_if_part_missing(tmp_path):
    """Verify finalize_multi checks all parts before renaming any.

    If a part is missing, it raises an error and leaves the session intact
    (no renames, metadata still present) so the caller can retry.
    """
    upload_id = uuid.uuid4()
    files_list = [
        {"filename": "a.mp4", "suffix": ".mp4", "total_size": 10, "last_modified": 1000},
        {"filename": "b.mp4", "suffix": ".mp4", "total_size": 20, "last_modified": 2000},
        {"filename": "c.mp4", "suffix": ".mp4", "total_size": 30, "last_modified": 3000},
    ]
    UploadSession.init_multi(
        tmp_path, "tester", user_id="u1", upload_id=upload_id, files=files_list,
        kind="video", options={}, display_name=None, created_at="2026-08-03T00:00:00+00:00",
    )
    meta_path = UploadSession.meta_path(tmp_path, "tester", upload_id)

    # Populate parts 0 and 2, but deliberately delete part 1's .part file
    for index in (0, 2):
        part = UploadSession.part_path_for(tmp_path, "tester", upload_id, index, ".mp4")
        UploadSession.append_chunk_at(part, meta_path, f"part{index}".encode(), index)

    # Delete part 1's staging file to simulate a missing part
    part1 = UploadSession.part_path_for(tmp_path, "tester", upload_id, 1, ".mp4")
    part1.unlink()

    meta = UploadSession.load(tmp_path, "tester", upload_id)

    # Attempt finalize; should fail because part 1 is missing
    try:
        UploadSession.finalize_multi(tmp_path, "tester", upload_id, meta)
        assert False, "Expected RuntimeError for missing part"
    except RuntimeError as e:
        assert "missing staging file" in str(e).lower()
        assert "audio.original.001.mp4.part" in str(e)

    # Session should remain intact and retryable:
    # - Parts 0 and 2 still exist as .part files (not renamed)
    # - Metadata still exists
    assert UploadSession.part_path_for(tmp_path, "tester", upload_id, 0, ".mp4").exists()
    assert UploadSession.part_path_for(tmp_path, "tester", upload_id, 2, ".mp4").exists()
    assert meta_path.exists()
    # Part 1 should not exist as a final file either
    part1_final = tmp_path / "tester" / str(upload_id) / "media" / "audio.original.001.mp4"
    assert not part1_final.exists()
