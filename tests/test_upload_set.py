"""Validation for a multi-file upload set (vts-vm0).

Two separate gates: the suffix split (cheap, runs at init before any bytes
move) and the probe check (authoritative, runs at finalize once bytes exist).
"""
from __future__ import annotations

import pytest

from vts.services.media import MediaProbe
from vts.services.upload_set import (
    UploadSetError,
    classify_suffixes,
    verify_probes,
)


def _probe(*, has_video=True, w=640, h=480, rate="25/1", vcodec="h264",
           acodec="aac", sr=44100, ch=1, duration=1.0):
    return MediaProbe(
        duration_sec=duration, creation_time=None, has_video=has_video,
        video_codec=vcodec if has_video else None,
        width=w if has_video else None, height=h if has_video else None,
        frame_rate=rate if has_video else None,
        audio_codec=acodec, sample_rate=sr, channels=ch,
    )


def test_all_video_classifies_as_video():
    assert classify_suffixes(["a.mp4", "b.mkv", "c.mov"]) == "video"


def test_all_audio_classifies_as_audio():
    assert classify_suffixes(["a.mp3", "b.opus", "c.wav"]) == "audio"


def test_mixed_set_is_rejected_naming_the_files():
    with pytest.raises(UploadSetError) as excinfo:
        classify_suffixes(["talk.mp4", "note.mp3"])
    message = str(excinfo.value)
    assert "talk.mp4" in message and "note.mp3" in message


def test_unsupported_suffix_is_rejected():
    with pytest.raises(UploadSetError, match="doc.pdf"):
        classify_suffixes(["a.mp4", "doc.pdf"])


def test_compatible_video_set_passes():
    verify_probes("video", [("a.mp4", _probe()), ("b.mp4", _probe())])


def test_video_set_rejected_on_resolution_mismatch():
    with pytest.raises(UploadSetError) as excinfo:
        verify_probes("video", [("a.mp4", _probe(w=640, h=480)), ("b.mp4", _probe(w=1280, h=720))])
    assert "b.mp4" in str(excinfo.value)


def test_video_set_rejected_on_frame_rate_mismatch():
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(rate="25/1")), ("b.mp4", _probe(rate="30/1"))])


def test_video_set_rejected_on_audio_sample_rate_mismatch():
    """ffmpeg reports NO error for this case — concat -c copy just produces a
    wrong-duration file (measured 4.38s for 2+2s), so we must catch it here."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(sr=44100)), ("b.mp4", _probe(sr=48000))])


def test_audio_set_ignores_video_parameters():
    """Audio parts are re-encoded to 16k mono anyway, so differing sample rates
    are fine — only video sets need matching parameters."""
    verify_probes("audio", [
        ("a.mp3", _probe(has_video=False, sr=44100)),
        ("b.wav", _probe(has_video=False, sr=48000)),
    ])


def test_video_suffix_without_video_stream_is_rejected():
    """An extension does not prove content: a .mkv may carry audio only."""
    with pytest.raises(UploadSetError, match="b.mkv"):
        verify_probes("video", [("a.mp4", _probe()), ("b.mkv", _probe(has_video=False))])


def test_audio_set_with_a_video_stream_is_rejected():
    with pytest.raises(UploadSetError, match="b.m4a"):
        verify_probes("audio", [("a.mp3", _probe(has_video=False)), ("b.m4a", _probe(has_video=True))])


def test_video_set_rejected_on_codec_mismatch():
    """Video codec must match across parts; a hypothetical bug comparing only
    resolution would miss this."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(vcodec="h264")), ("b.mp4", _probe(vcodec="vp9"))])


def test_video_set_rejected_on_audio_codec_mismatch():
    """Audio codec must match across video parts; audio re-encoding is NOT
    automatic, so codec mismatch is a concat error."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(acodec="aac")), ("b.mp4", _probe(acodec="opus"))])


def test_video_set_rejected_on_channel_count_mismatch():
    """Channel count must match across video parts."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(ch=1)), ("b.mp4", _probe(ch=2))])


def test_video_set_rejected_on_width_mismatch_only():
    """Width must match; a bug comparing only height would miss this."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(w=640, h=480)), ("b.mp4", _probe(w=1280, h=480))])


def test_video_set_rejected_on_height_mismatch_only():
    """Height must match; a bug comparing only width would miss this."""
    with pytest.raises(UploadSetError):
        verify_probes("video", [("a.mp4", _probe(w=640, h=480)), ("b.mp4", _probe(w=640, h=720))])


def test_three_part_video_set_rejects_third_file_on_mismatch():
    """All files are checked against the first; a hypothetical bug that only
    compares probes[0] vs probes[1] would not catch a third-file mismatch."""
    with pytest.raises(UploadSetError) as excinfo:
        verify_probes("video", [
            ("a.mp4", _probe(w=640, h=480)),
            ("b.mp4", _probe(w=640, h=480)),
            ("c.mp4", _probe(w=1280, h=720)),
        ])
    assert "c.mp4" in str(excinfo.value)
