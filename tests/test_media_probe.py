from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vts.services.media import probe_duration, trim_initial_silence

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _make_silent_wav(path: Path, seconds: float) -> None:
    # anullsrc emits true digital silence — silenceremove will strip it entirely.
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            str(seconds),
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_empty_wav(path: Path) -> None:
    # Stripping all audio yields a 0-sample WAV — ffprobe reports no format.duration.
    src = path.with_name("src.wav")
    _make_silent_wav(src, 1.0)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            "silenceremove=start_periods=1:start_duration=0.5:start_threshold=-50dB:start_mode=all",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_probe_duration_returns_zero_for_empty_wav(tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    _make_empty_wav(empty)
    # Regression: ffprobe returns {"format": {}} with no "duration" key for a
    # zero-sample WAV. probe_duration must degrade to 0.0, not raise KeyError.
    assert probe_duration(empty) == 0.0


def test_trim_initial_silence_falls_back_when_everything_is_silence(
    tmp_path: Path,
) -> None:
    silent = tmp_path / "audio_16k.wav"
    _make_silent_wav(silent, 10.0)
    output = tmp_path / "audio_16k_trimmed.wav"
    log = tmp_path / "ffmpeg.log"

    trimmed = trim_initial_silence(
        silent,
        output,
        log,
        threshold_db=-50.0,
        min_duration_sec=0.5,
        max_trim_seconds=30.0,
    )

    # Whole clip was silence → silenceremove empties the output. The guard must
    # copy the original back and report no trim, not crash on the empty probe.
    assert trimmed == 0.0
    assert output.exists()
    assert probe_duration(output) > 0.0
