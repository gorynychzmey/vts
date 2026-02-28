from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, TypedDict


class SegmentSpec(TypedDict):
    segment_index: int
    start: float
    end: float
    file: str


def run_ffmpeg(command: list[str], log_path: Path | None = None) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            if proc.stdout:
                f.write(proc.stdout + "\n")
            if proc.stderr:
                f.write(proc.stderr + "\n")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(command)}")


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}")
    payload = json.loads(proc.stdout)
    return float(payload["format"]["duration"])


def extract_audio_16k_mono(input_file: Path, output_wav: Path, log_path: Path) -> None:
    if output_wav.exists():
        return
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]
    run_ffmpeg(cmd, log_path)


def trim_initial_silence(
    input_wav: Path,
    output_wav: Path,
    log_path: Path,
    *,
    threshold_db: float,
    min_duration_sec: float,
    max_trim_seconds: float,
) -> float:
    input_duration = probe_duration(input_wav)
    if output_wav.exists():
        output_duration = probe_duration(output_wav)
        return max(0.0, input_duration - output_duration)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_wav),
        "-af",
        (
            "silenceremove="
            f"start_periods=1:start_duration={min_duration_sec}:start_threshold={threshold_db}dB:start_mode=all"
        ),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]
    run_ffmpeg(cmd, log_path)
    output_duration = probe_duration(output_wav)
    trimmed = max(0.0, input_duration - output_duration)
    if output_duration <= 0.0 or trimmed > max_trim_seconds:
        shutil.copy2(input_wav, output_wav)
        return 0.0
    return trimmed


def detect_silence_points(audio_wav: Path, log_path: Path, search_window: int) -> list[float]:
    cmd = [
        "ffmpeg",
        "-i",
        str(audio_wav),
        "-af",
        "silencedetect=noise=-30dB:d=1.0",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    with log_path.open("a", encoding="utf-8") as logf:
        if proc.stderr:
            logf.write(proc.stderr + "\n")
    silence_points: list[float] = []
    pattern = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")
    for line in (proc.stderr or "").splitlines():
        match = pattern.search(line)
        if match:
            point = float(match.group(1))
            silence_points.append(point)
    # Keep only points that can be useful for adjustment around target windows.
    deduped: list[float] = []
    for point in silence_points:
        if not deduped or abs(point - deduped[-1]) > max(search_window / 10.0, 1.0):
            deduped.append(point)
    return deduped


def build_segments(
    *,
    duration_sec: float,
    target_seconds: int,
    search_window_seconds: int,
    overlap_seconds: int,
    silence_points: list[float],
) -> list[tuple[float, float]]:
    if duration_sec <= 0:
        return []
    boundaries = [0.0]
    cursor = float(target_seconds)
    while cursor < duration_sec:
        lower = max(0.0, cursor - search_window_seconds)
        upper = min(duration_sec, cursor + search_window_seconds)
        candidates = [p for p in silence_points if lower <= p <= upper]
        chosen = min(candidates, key=lambda p: abs(p - cursor)) if candidates else cursor
        if chosen - boundaries[-1] < 45:
            chosen = min(duration_sec, boundaries[-1] + target_seconds)
        boundaries.append(chosen)
        cursor = chosen + target_seconds
    boundaries.append(duration_sec)

    segments: list[tuple[float, float]] = []
    for idx in range(len(boundaries) - 1):
        start = boundaries[idx]
        end = boundaries[idx + 1]
        ext_start = max(0.0, start - (overlap_seconds if idx > 0 else 0))
        ext_end = min(duration_sec, end + (overlap_seconds if idx < len(boundaries) - 2 else 0))
        segments.append((ext_start, ext_end))
    return segments


def export_segments(
    audio_wav: Path,
    segments: list[tuple[float, float]],
    segment_dir: Path,
    log_path: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[SegmentSpec]:
    segment_dir.mkdir(parents=True, exist_ok=True)
    specs: list[SegmentSpec] = []
    total = len(segments)
    for idx, (start, end) in enumerate(segments, start=1):
        segment_file = segment_dir / f"{idx:04d}.wav"
        if not segment_file.exists():
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_wav),
                "-ss",
                str(start),
                "-to",
                str(end),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(segment_file),
            ]
            run_ffmpeg(cmd, log_path)
        specs.append(
            SegmentSpec(
                segment_index=idx,
                start=start,
                end=end,
                file=str(segment_file.name),
            )
        )
        if progress_cb is not None:
            progress_cb(idx, total)
    return specs
