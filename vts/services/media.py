from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
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
        # Include the tail of stderr: without it the exception said only that
        # ffmpeg failed, and the actual reason lived in a log file that the
        # error message did not name (vts-c58).
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
        detail = f"\n{tail}" if tail else ""
        raise RuntimeError(f"ffmpeg failed: {' '.join(command)}{detail}")


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
    # A zero-sample WAV (e.g. silenceremove stripped all audio) yields
    # {"format": {}} with no "duration" key. Treat that as zero length so
    # callers' empty-output guards can engage instead of crashing.
    raw = payload.get("format", {}).get("duration")
    if raw is None:
        return 0.0
    return float(raw)


@dataclass(frozen=True)
class MediaProbe:
    """What one ffprobe call tells us about an uploaded file (vts-vm0).

    Ordering wants `creation_time`; video concat compatibility wants the stream
    parameters. One probe serves both.
    """

    duration_sec: float
    creation_time: str | None
    has_video: bool
    video_codec: str | None
    width: int | None
    height: int | None
    frame_rate: str | None
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None

    def video_signature(self) -> tuple:
        return (self.video_codec, self.width, self.height, self.frame_rate)

    def audio_signature(self) -> tuple:
        return (self.audio_codec, self.sample_rate, self.channels)


def probe_media(path: Path) -> MediaProbe:
    """Inspect `path` with a single ffprobe call.

    Raises RuntimeError when ffprobe cannot read the file — the caller turns
    that into a task failure naming the file (vts-vm0).
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:format_tags=creation_time"
                         ":stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffprobe failed for {path.name}{': ' + tail if tail else ''}")
    payload = json.loads(proc.stdout or "{}")
    fmt = payload.get("format", {}) or {}
    streams = payload.get("streams", []) or []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    raw_duration = fmt.get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return MediaProbe(
        duration_sec=duration,
        creation_time=(fmt.get("tags") or {}).get("creation_time"),
        has_video=video is not None,
        video_codec=(video or {}).get("codec_name"),
        width=_int((video or {}).get("width")),
        height=_int((video or {}).get("height")),
        frame_rate=(video or {}).get("r_frame_rate"),
        audio_codec=(audio or {}).get("codec_name"),
        sample_rate=_int((audio or {}).get("sample_rate")),
        channels=_int((audio or {}).get("channels")),
    )


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


def _write_concat_list(inputs: list[Path], list_path: Path) -> None:
    """ffmpeg's concat demuxer input list. Single quotes are escaped per its
    own quoting rules, so a filename containing one cannot break the list."""
    lines = []
    for item in inputs:
        escaped = str(item.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_to_audio_16k_mono(
    inputs: list[Path], output_wav: Path, log_path: Path, work_dir: Path
) -> list[float]:
    """Normalise each input to 16 kHz mono PCM, then join in the given order.

    Returns each input's duration, in the same order, so the caller can record
    file-boundary offsets. Normalising first is what makes heterogeneous
    sources safe to concatenate: verified, mp3 44.1k stereo + wav 48k mono +
    ogg 22k stereo of 2+3+1s produce exactly 6.000s (vts-vm0).
    """
    if not inputs:
        raise RuntimeError("No input files to concatenate")

    work_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    durations: list[float] = []
    for index, item in enumerate(inputs):
        target = work_dir / f"norm_{index:03d}.wav"
        if not target.exists():
            extract_audio_16k_mono(item, target, log_path)
        normalized.append(target)
        durations.append(probe_duration(target))

    if len(normalized) == 1:
        shutil.copyfile(normalized[0], output_wav)
        return durations

    list_path = work_dir / "concat_audio.txt"
    _write_concat_list(normalized, list_path)
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(output_wav)],
        log_path,
    )
    return durations


def concat_video_stream_copy(
    inputs: list[Path], output: Path, log_path: Path, work_dir: Path
) -> None:
    """Join video parts without re-encoding.

    Callers MUST have verified parameter compatibility first
    (vts.services.upload_set.verify_probes): stream-copying mismatched inputs
    silently produces a wrong-duration file, and for differing audio sample
    rates ffmpeg does not even report an error (vts-vm0).
    """
    if not inputs:
        raise RuntimeError("No input files to concatenate")

    work_dir.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1:
        run_ffmpeg(["ffmpeg", "-y", "-i", str(inputs[0]), "-c", "copy", str(output)], log_path)
        return

    list_path = work_dir / "concat_video.txt"
    _write_concat_list(inputs, list_path)
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(output)],
        log_path,
    )


def concat_audio_stream_copy(
    inputs: list[Path], output: Path, log_path: Path, work_dir: Path
) -> None:
    """Join audio parts without re-encoding, preserving the original quality.

    The transcription pipeline needs 16 kHz mono, but that is not what the user
    uploaded and not what they should have to listen to. When the parts share a
    codec this joins them as-is, so the player serves the original encoding
    (vts-08q).

    Codec is the ONLY hard requirement, and it is checked here rather than by a
    caller — unlike `concat_video_stream_copy`, whose compatibility gate runs at
    finalize. Measured on real files:

      - same codec, differing sample rate/channels: duration correct, pitch
        shifts ~0.7% (an 880 Hz tone came back at 874 Hz) — inaudible, and far
        better than dropping the whole set to 16 kHz mono.
      - DIFFERENT codecs (opus + mp3): 1181 seconds of output for 4 seconds of
        input, with NO ffmpeg error whatsoever.

    That last case is why this raises on a codec mismatch instead of trusting
    ffmpeg's exit code. Callers are expected to catch ValueError and fall back
    to the normalised 16 kHz mono concat — a mismatch is a quality question for
    audio, never a reason to reject the upload.
    """
    if not inputs:
        raise RuntimeError("No input files to concatenate")

    codecs = [(item, probe_media(item).audio_codec) for item in inputs]
    first_name, first_codec = codecs[0]
    for name, codec in codecs[1:]:
        if codec != first_codec:
            raise ValueError(
                f"{name.name} is {codec}, but {first_name.name} is {first_codec}. "
                "Audio parts must share a codec to be joined without re-encoding."
            )

    work_dir.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1:
        shutil.copyfile(inputs[0], output)
        return

    list_path = work_dir / "concat_audio_copy.txt"
    _write_concat_list(inputs, list_path)
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
         "-c", "copy", str(output)],
        log_path,
    )
