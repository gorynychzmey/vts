"""ffprobe-backed media inspection (vts-vm0).

Ordering and video-compatibility both need facts about the file that only
ffprobe can supply, so they share one probe rather than shelling out twice.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vts.services.media import MediaProbe, probe_media


def _make(path: Path, *, video: bool, seconds: int = 1, size: str = "320x240",
          rate: int = 25, sample_rate: int = 44100, creation: str | None = None) -> Path:
    cmd = ["ffmpeg", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate={sample_rate}"]
    # Each container takes its own codecs: WebM permits only VP8/VP9 with
    # Vorbis/Opus, and wav is PCM. Muxing h264+aac into webm produces no file
    # at all, which is how an earlier measurement mistook "could not build the
    # file" for "the container cannot store creation_time".
    name = str(path)
    if video:
        if name.endswith(".webm"):
            cmd += ["-c:v", "libvpx-vp9"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast"]
    if name.endswith(".webm"):
        cmd += ["-c:a", "libvorbis"]
    elif name.endswith(".wav"):
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac"]
    cmd += ["-ar", str(sample_rate), "-shortest"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def test_probe_reads_duration_and_streams_for_video(tmp_path):
    f = _make(tmp_path / "v.mp4", video=True, seconds=2, size="640x480", rate=25)
    probe = probe_media(f)
    assert probe.has_video is True
    assert probe.width == 640 and probe.height == 480
    assert probe.video_codec == "h264"
    assert probe.audio_codec == "aac"
    assert probe.sample_rate == 44100
    assert 1.8 < probe.duration_sec < 2.3


def test_probe_reads_audio_only_file(tmp_path):
    f = _make(tmp_path / "a.m4a", video=False, seconds=1)
    probe = probe_media(f)
    assert probe.has_video is False
    assert probe.width is None and probe.height is None
    assert probe.audio_codec == "aac"


def test_probe_reads_creation_time_when_present(tmp_path):
    f = _make(tmp_path / "c.mp4", video=True, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time == "2026-08-01T10:00:00.000000Z"


def test_probe_returns_none_creation_time_when_absent(tmp_path):
    """A container that genuinely cannot store the tag must probe as None.

    wav is used because it really does drop creation_time: the assertion below
    holds even though the tag is explicitly SET on the ffmpeg command line, so
    the test cannot pass merely because nobody asked for the tag.

    (An earlier version used webm and did not set the tag. That passed for the
    wrong reason — webm stores creation_time perfectly well; the fixture simply
    never wrote one, so the test would also have passed against a probe_media
    that ignored the field entirely.)
    """
    f = _make(tmp_path / "c.wav", video=False, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time is None


def test_probe_reads_creation_time_from_webm(tmp_path):
    """webm DOES carry creation_time — pinned because an earlier measurement
    claimed otherwise and the spec's container table was wrong as a result."""
    f = _make(tmp_path / "c.webm", video=True, creation="2026-08-01T10:00:00.000000Z")
    assert probe_media(f).creation_time == "2026-08-01T10:00:00.000000Z"


def test_signatures_match_for_identical_parameters(tmp_path):
    a = _make(tmp_path / "a.mp4", video=True, size="640x480", rate=25)
    b = _make(tmp_path / "b.mp4", video=True, size="640x480", rate=25)
    pa, pb = probe_media(a), probe_media(b)
    assert pa.video_signature() == pb.video_signature()
    assert pa.audio_signature() == pb.audio_signature()


def test_video_signature_differs_on_resolution(tmp_path):
    a = _make(tmp_path / "a.mp4", video=True, size="640x480")
    b = _make(tmp_path / "b.mp4", video=True, size="1280x720")
    assert probe_media(a).video_signature() != probe_media(b).video_signature()


def test_audio_signature_differs_on_sample_rate(tmp_path):
    """This is the case ffmpeg does NOT report as an error — concat -c copy
    silently produces a wrong-duration file (measured: 4.38s for 2+2s)."""
    a = _make(tmp_path / "a.mp4", video=True, sample_rate=44100)
    b = _make(tmp_path / "b.mp4", video=True, sample_rate=48000)
    assert probe_media(a).audio_signature() != probe_media(b).audio_signature()


def test_probe_raises_on_unreadable_file(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a media file")
    with pytest.raises(RuntimeError, match="ffprobe"):
        probe_media(bad)
