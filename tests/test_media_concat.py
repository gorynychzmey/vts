"""Concatenating uploaded parts into the single artefacts the pipeline uses.

Durations are the assertion that matters: stream-copy concat of mismatched
inputs produces a WRONG DURATION without ffmpeg raising an error, so a test
that only checked the exit code would pass on a broken file (vts-vm0).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from vts.services.media import (
    concat_to_audio_16k_mono,
    concat_video_stream_copy,
    probe_duration,
)


def _audio(path: Path, seconds: int, freq: int, sample_rate: int, channels: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={sample_rate}",
         "-ac", str(channels), str(path)],
        capture_output=True, check=True,
    )
    return path


def _video(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "44100",
         "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


def test_audio_concat_sums_durations_across_formats(tmp_path):
    """Heterogeneous inputs join losslessly once normalised — 2+3+1 = 6."""
    a = _audio(tmp_path / "a.mp3", 2, 440, 44100, 2)
    b = _audio(tmp_path / "b.wav", 3, 880, 48000, 1)
    c = _audio(tmp_path / "c.ogg", 1, 660, 22050, 2)
    out = tmp_path / "audio_16k.wav"

    durations = concat_to_audio_16k_mono([a, b, c], out, tmp_path / "log.txt", tmp_path)

    assert out.exists()
    assert abs(probe_duration(out) - 6.0) < 0.2
    assert len(durations) == 3
    assert abs(durations[0] - 2.0) < 0.2
    assert abs(sum(durations) - 6.0) < 0.2


def test_audio_concat_output_is_16k_mono(tmp_path):
    a = _audio(tmp_path / "a.mp3", 1, 440, 44100, 2)
    out = tmp_path / "audio_16k.wav"
    concat_to_audio_16k_mono([a], out, tmp_path / "log.txt", tmp_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert "16000" in probe.stdout and probe.stdout.strip().endswith("1")


def test_video_concat_sums_durations(tmp_path):
    a = _video(tmp_path / "a.mp4", 2, 440)
    b = _video(tmp_path / "b.mp4", 2, 880)
    out = tmp_path / "video.mkv"

    concat_video_stream_copy([a, b], out, tmp_path / "log.txt", tmp_path)

    assert out.exists()
    assert abs(probe_duration(out) - 4.0) < 0.3


def test_video_concat_preserves_the_video_stream(tmp_path):
    a = _video(tmp_path / "a.mp4", 1, 440)
    out = tmp_path / "video.mkv"
    concat_video_stream_copy([a], out, tmp_path / "log.txt", tmp_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert "video" in probe.stdout


def test_audio_concat_respects_input_order(tmp_path):
    """Order in equals order out — the whole ordering feature depends on it."""
    short = _audio(tmp_path / "short.wav", 1, 440, 16000, 1)
    long = _audio(tmp_path / "long.wav", 3, 880, 16000, 1)
    out = tmp_path / "audio_16k.wav"
    durations = concat_to_audio_16k_mono([long, short], out, tmp_path / "log.txt", tmp_path)
    assert abs(durations[0] - 3.0) < 0.2
    assert abs(durations[1] - 1.0) < 0.2
