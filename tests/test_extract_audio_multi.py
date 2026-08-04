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


class _Bus:
    """Minimal async no-op event bus that records what was published.

    ExtractAudioStep's single-file path publishes a phase/done event on the
    real bus (vts.pipeline.context.PipelineContext.bus) — that call must keep
    working, since app.js:3387 unconditionally clears runtime.mediaPhase on
    receipt, which gates the download/media progress display (app.js:1323,
    app.js:1360). A fake ctx with no .bus at all would mask a regression that
    silently deletes this call, so the fake mirrors the real shape instead of
    omitting it.
    """

    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish_event(self, *, user_id, task_id, event, data, **kwargs):
        self.published.append({"user_id": user_id, "task_id": task_id, "event": event, "data": data})


class _Ctx:
    class settings:
        timezone = "UTC"

    def task_flag(self, options, key, *, default):
        return default

    def __init__(self):
        self.persisted_options: list[dict] = []
        self.bus = _Bus()

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
    # A gating typo (e.g. `!= "audio"`) on the video branch must not slip
    # through silently — an audio-kind set must never produce video.mkv.
    assert not (dirs["media"] / "video.mkv").exists()


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
    # A gating typo (e.g. `!= "audio"`) on the video branch must not slip
    # through silently — an audio-kind set must never produce video.mkv.
    assert not (dirs["media"] / "video.mkv").exists()


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
async def test_partial_failure_leaves_no_completion_marker(tmp_path, monkeypatch):
    """A failure part-way through the multi-file branch must be retriable.

    If video.mkv building blows up (ffmpeg error, disk full, OOM kill), the
    step must not have already written audio_16k.wav — that file is the
    completion marker already_done()/run()'s own early-return guard checks.
    Leaving it behind after a partial failure would make the retry silently
    skip the whole step: video.mkv never gets built (so the player falls
    back to one arbitrary raw part) and the DB keeps Task 7's 0.0 offset
    placeholders forever, with nothing reporting an error (vts-vm0).
    """
    import vts.pipeline.steps.media as media_module
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
    ctx = _Ctx()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ffmpeg failure building video.mkv")

    monkeypatch.setattr(media_module, "concat_video_stream_copy", _boom)

    with pytest.raises(RuntimeError, match="simulated ffmpeg failure"):
        await ExtractAudioStep().run(ctx, st)

    # The exception must propagate (not be swallowed)...
    assert not (dirs["media"] / "audio_16k.wav").exists(), (
        "audio_16k.wav must not exist after a partial failure — its presence "
        "would make a retry's early-return guard declare the step done"
    )
    assert not (dirs["media"] / "video.mkv").exists()
    # ...and options must not have been persisted with placeholder-replacing
    # values the pipeline never actually committed to disk.
    assert not ctx.persisted_options, "persist_task_options must not run before all work succeeds"
    # The work dir must not be leaked on the failure path either.
    assert not (dirs["media"] / "concat_work").exists()


@pytest.mark.asyncio
async def test_audio_set_produces_combined_artefact_for_playback(tmp_path):
    """An AUDIO set must end up with ONE combined playable artefact, mirroring
    video.mkv for video sets (blocker 1, vts-vm0 final review).

    Without this, _find_media_file's only candidate for an audio set is the
    raw `audio.original.NNN.*` parts, and its glob-then-[-1] resolution
    serves only the highest-numbered part — playback (and vts-at8 transcript
    seeking, and the stats block's media_seconds/media_bytes) then covers
    only the last part instead of the whole recording.
    """
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

    from vts.api.main import _find_media_file

    resolved = _find_media_file(str(tmp_path))
    assert resolved is not None
    assert resolved.name not in {
        "audio.original.000.wav", "audio.original.001.wav",
    }, (
        f"_find_media_file resolved a raw part ({resolved.name}) instead of "
        "a combined artefact covering the whole set"
    )
    assert abs(probe_duration(resolved) - 5.0) < 0.3, (
        "the resolved file must cover the full 5s set, not just one part"
    )


@pytest.mark.asyncio
async def test_audio_partial_failure_leaves_no_combined_artefact(tmp_path, monkeypatch):
    """Same crash-safety guarantee as the video branch's
    test_partial_failure_leaves_no_completion_marker, applied to the new
    audio.combined.wav artefact: a failure after it is written but before
    the step's own completion marker (audio_16k.wav) must not leave a stray
    combined file behind for a retry to trip over.
    """
    import vts.pipeline.steps.media as media_module
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
    ctx = _Ctx()

    # The combined artefact is now produced by concat_audio_stream_copy
    # (vts-08q), so that is what has to fail here — patching shutil.copyfile
    # would no longer be reached on the happy path.
    def _boom(*a, **kw):
        raise RuntimeError("simulated crash building the combined audio")

    monkeypatch.setattr(media_module, "concat_audio_stream_copy", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await ExtractAudioStep().run(ctx, st)

    assert not list(dirs["media"].glob("audio.combined.*"))
    assert not (dirs["media"] / "audio_16k.wav").exists(), (
        "audio_16k.wav must not exist after a partial failure — its presence "
        "would make a retry's early-return guard declare the step done"
    )
    assert not ctx.persisted_options
    assert not (dirs["media"] / "concat_work").exists()


@pytest.mark.asyncio
async def test_single_file_task_is_unaffected(tmp_path):
    """No source_files in options means the original single-file behaviour."""
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    _audio(dirs["media"] / "audio.original.wav", 2, 440)

    st = _State(dirs, {})
    ctx = _Ctx()
    await ExtractAudioStep().run(ctx, st)

    out = dirs["media"] / "audio_16k.wav"
    assert out.exists()
    assert abs(probe_duration(out) - 2.0) < 0.3

    # The frontend clears runtime.mediaPhase unconditionally on receipt of
    # this event (app.js:3387), which gates the download/media progress
    # display (app.js:1323, app.js:1360). Do not delete this publish call —
    # it is not dead code just because status:"done" skips the "running"
    # branch above it in patchTaskPhase.
    phase_done_events = [
        e for e in ctx.bus.published
        if e["event"] == "phase" and e["data"].get("phase") == "extract_audio"
    ]
    assert phase_done_events, "single-file extract_audio must publish a phase/done event"
    assert phase_done_events[-1]["data"]["status"] == "done"


@pytest.mark.asyncio
async def test_audio_set_falls_back_when_codecs_differ(tmp_path, monkeypatch):
    """Mismatched codecs must degrade to 16 kHz mono, not reject the upload.

    Video sets refuse an incompatible set; audio must not, because the
    transcription pipeline normalises everything anyway — the only thing at
    stake is playback quality (vts-08q).
    """
    import subprocess

    import vts.pipeline.steps.media as media_module
    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    # Deliberately different codecs: opus + mp3. Stream-copying these produces
    # a 1181-second file with no ffmpeg error, which is why the guard exists.
    for name, codec, freq in (
        ("audio.original.000.opus", "libopus", 440),
        ("audio.original.001.mp3", "libmp3lame", 880),
    ):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             f"-i", f"sine=frequency={freq}:duration=2", "-c:a", codec,
             str(dirs["media"] / name)],
            capture_output=True, check=True,
        )

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.opus", "offset_sec": 0.0, "duration_sec": 2.0},
            {"name": "b.mp3", "offset_sec": 0.0, "duration_sec": 2.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    # The upload is NOT rejected: the step completes and produces the fallback.
    combined = list(dirs["media"].glob("audio.combined.*"))
    assert len(combined) == 1, combined
    assert combined[0].name == "audio.combined.wav", (
        "a codec mismatch must fall back to the normalised concat, not "
        f"stream-copy into {combined[0].name}"
    )
    # And the duration is right — the broken 1181s concat must not have run.
    assert abs(probe_duration(combined[0]) - 4.0) < 0.3


@pytest.mark.asyncio
async def test_audio_set_stream_copies_when_codecs_match(tmp_path):
    """The point of the feature: same-codec parts keep their own encoding."""
    import subprocess

    from vts.pipeline.steps.media import ExtractAudioStep

    dirs = ensure_task_dirs(tmp_path)
    for index, (name, freq) in enumerate(
        (("audio.original.000.opus", 440), ("audio.original.001.opus", 880))
    ):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             f"-i", f"sine=frequency={freq}:duration=2:sample_rate=48000",
             "-ac", "2", "-c:a", "libopus", str(dirs["media"] / name)],
            capture_output=True, check=True,
        )

    options = {
        "source_files_kind": "audio",
        "source_files": [
            {"name": "a.opus", "offset_sec": 0.0, "duration_sec": 2.0},
            {"name": "b.opus", "offset_sec": 0.0, "duration_sec": 2.0},
        ],
    }
    st = _State(dirs, options)
    await ExtractAudioStep().run(_Ctx(), st)

    combined = list(dirs["media"].glob("audio.combined.*"))
    assert len(combined) == 1, combined
    assert combined[0].name == "audio.combined.opus", (
        "matching codecs must be stream-copied, preserving the upload's "
        f"encoding — got {combined[0].name}"
    )
    assert abs(probe_duration(combined[0]) - 4.0) < 0.3
