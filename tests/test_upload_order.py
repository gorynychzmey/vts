"""Concat order for a multi-file upload (vts-vm0).

Order is decided server-side with no user step, so the fallback chain has to be
right: container creation_time, then the browser's lastModified, then natural
filename sort. Measured container coverage is in the spec — ogg/opus/wav and
avi/wmv/ts carry no creation_time at all, and opus is what Telegram and
WhatsApp voice messages use, so the filename rule is load-bearing.
"""
from __future__ import annotations

from vts.services.upload_order import natural_key, resolve_order


def _e(filename, creation_time=None, last_modified=None):
    return {"filename": filename, "creation_time": creation_time, "last_modified": last_modified}


def test_creation_time_wins_when_all_present():
    entries = [
        _e("b.mp4", creation_time="2026-08-01T10:05:00.000000Z", last_modified=999),
        _e("a.mp4", creation_time="2026-08-01T10:00:00.000000Z", last_modified=1),
    ]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.mp4", "b.mp4"]
    assert source == "creation_time"


def test_falls_back_to_last_modified_when_any_creation_time_missing():
    entries = [
        _e("b.mp4", creation_time="2026-08-01T10:05:00.000000Z", last_modified=2000),
        _e("a.opus", creation_time=None, last_modified=1000),
    ]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.opus", "b.mp4"]
    assert source == "last_modified"


def test_falls_back_to_natural_filename_when_no_dates():
    entries = [_e("rec_10.opus"), _e("rec_9.opus"), _e("rec_1.opus")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["rec_1.opus", "rec_9.opus", "rec_10.opus"]
    assert source == "filename"


def test_natural_sort_beats_lexicographic():
    names = ["part10.wav", "part2.wav", "part1.wav"]
    assert sorted(names, key=natural_key) == ["part1.wav", "part2.wav", "part10.wav"]
    assert sorted(names) == ["part1.wav", "part10.wav", "part2.wav"]


def test_telegram_and_whatsapp_names_order_correctly():
    tg = [_e("audio_2026-08-01_10-15-03.ogg"), _e("audio_2026-08-01_09-58-11.ogg")]
    assert [e["filename"] for e in resolve_order(tg)[0]] == [
        "audio_2026-08-01_09-58-11.ogg", "audio_2026-08-01_10-15-03.ogg",
    ]
    wa = [_e("PTT-20260801-WA0011.opus"), _e("PTT-20260801-WA0002.opus")]
    assert [e["filename"] for e in resolve_order(wa)[0]] == [
        "PTT-20260801-WA0002.opus", "PTT-20260801-WA0011.opus",
    ]


def test_identical_last_modified_falls_through_to_filename():
    """Downloading a set in one go gives every file the same mtime — that is no
    signal at all, so it must not be treated as an order."""
    entries = [_e("rec_10.opus", last_modified=5000), _e("rec_9.opus", last_modified=5000)]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["rec_9.opus", "rec_10.opus"]
    assert source == "filename"


def test_single_file_is_returned_unchanged():
    entries = [_e("only.mp4")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["only.mp4"]
    assert source == "filename"


def test_unparseable_creation_time_falls_back():
    entries = [_e("b.mp4", creation_time="not-a-date"), _e("a.mp4", creation_time="also-bad")]
    ordered, source = resolve_order(entries)
    assert [e["filename"] for e in ordered] == ["a.mp4", "b.mp4"]
    assert source == "filename"
