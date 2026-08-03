"""extract_audio joins a multi-file set (vts-vm0).

Everything after this step reads only audio_16k.wav, so concatenating here
leaves the rest of the pipeline untouched. A video set additionally needs
video.mkv, because the player resolves media via _find_media_file.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from vts.services.media import probe_duration
from vts.services.storage import ensure_task_dirs


def _audio(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=16000",
         "-ac", "1", str(path)],
        capture_output=True, check=True,
    )
    return path


def _video(path: Path, seconds: int, freq: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
        capture_output=True, check=True,
    )
    return path


class _Ctx:
    class settings:
        timezone = "UTC"

    def task_flag(self, options, key, *, default):
        return default

    def __init__(self):
        self.persisted_options: list[dict] = []

    async def persist_task_options(self, task_id, options):
        self.persisted_options.append(options)


class _State:
    def __init__(self, dirs, options):
        self.dirs = dirs
        self.task_options = options
        self.task_id = uuid.uuid4()
        self.user_id = "u1"
        import logging
        self.logger = logging.getLogger("test.extract")


@pytest.mark.asyncio
async def test_audio_set_is_concatenated(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.000.wav", 2, 440)
    _audio(dirs["media"] / "audio.original.001.wav", 3, 880)

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.wav", "offset_sec": 0.0, "duration_sec": 2.0},
            {"name": "b.wav", "offset_sec": 0.0, "duration_sec": 3.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    out = dirs["media"] / "audio_16k.wav"
    assert out.exists()
    assert abs(probe_duration(out) - 5.0) < 0.3


@pytest.mark.asyncio
async def test_offsets_are_recorded(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.000.wav", 2, 440)
    _audio(dirs["media"] / "audio.original.001.wav", 3, 880)

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.wav", "offset_sec": 0.0, "duration_sec": 0.0},
            {"name": "b.wav", "offset_sec": 0.0, "duration_sec": 0.0},
        ],
    }
    st = _State(dirs, options)
    ctx = _Ctx()
    await ExtractAudioStep().run(ctx, st)

    files = st.task_options["source_files"]
    assert abs(files[0]["offset_sec"] - 0.0) < 0.01
    assert abs(files[1]["offset_sec"] - 2.0) < 0.3

    # The offsets must reach the DB, not just live in memory (vts-vm0).
    assert ctx.persisted_options, "persist_task_options was never called"
    persisted = ctx.persisted_options[-1]
    assert abs(persisted["source_files"][1]["offset_sec"] - 2.0) < 0.3


@pytest.mark.asyncio
async def test_video_set_also_builds_video_mkv(tmp_path):
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _video(dirs["media"] / "audio.original.000.mp4", 1, 440)
    _video(dirs["media"] / "audio.original.001.mp4", 1, 880)

    options = {
        "source_files_kind": "video",
        "source_files": [
            {"name": "a.mp4", "offset_sec": 0.0, "duration_sec": 1.0},
            {"name": "b.mp4", "offset_sec": 0.0, "duration_sec": 1.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    assert (dirs["media"] / "audio_16k.wav").exists()
    video = dirs["media"] / "video.mkv"
    assert video.exists(), "a video set must produce a combined video for the player"
    assert abs(probe_duration(video) - 2.0) < 0.4


@pytest.mark.asyncio
async def test_single_file_task_is_unaffected(tmp_path):
    """No source_files in options means the original single-file behaviour."""
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.wav", 2, 440)

    st = _State(dirs, {})
    await ExtractAudioStep().run(_Ctx(), st)

    out = dirs["media"] / "audio_16k.wav"
    assert out.exists()
    assert abs(probe_duration(out) - 2.0) < 0.3
