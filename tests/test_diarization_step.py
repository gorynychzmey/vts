import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.diarization import DiarizeStep, select_preview_spans


def _write_silent_wav(path: Path, seconds: float = 5.0) -> None:
    # DiarizeStep now cuts speaker preview clips with real ffmpeg after writing
    # diarization.json (vts-80i), so tests exercising the full run() need a
    # real, ffmpeg-readable wav rather than a `b"RIFF"` stub.
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


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.job_ids: list[str | None] = []
        self.cancelled: list[str] = []

    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
        *,
        job_id: str | None = None,
        on_progress=None,
    ) -> dict:
        self.calls.append(audio_path)
        self.job_ids.append(job_id)
        if on_progress is not None:
            await on_progress("embeddings", 1, 2)
        return {
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [0.1, 0.2]},
            "num_speakers": 1,
        }

    async def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


class _FakeBus:
    """Records published events; cancellation off unless a test asks for it."""

    def __init__(self, cancel: bool = False, pause: bool = False) -> None:
        self.events: list[dict] = []
        self._cancel = cancel
        self._pause = pause

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    async def is_cancel_requested(self, task_id) -> bool:
        return self._cancel

    async def is_pause_requested(self, task_id) -> bool:
        return self._pause


def _dirs(tmp_path: Path) -> dict[str, Path]:
    for name in ("media", "outputs", "segments", "logs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return {name: tmp_path / name for name in ("media", "outputs", "segments", "logs")}


def _ctx(backend: _FakeBackend, bus: "_FakeBus | None" = None) -> SimpleNamespace:
    def transcribe_audio_path(dirs: dict[str, Path]) -> Path:
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        return trimmed if trimmed.exists() else dirs["media"] / "audio_16k.wav"

    return SimpleNamespace(
        diarization=backend,
        transcribe_audio_path=transcribe_audio_path,
        settings=SimpleNamespace(
            diarization_enabled_default=False,
            speaker_preview_count=3,
            speaker_preview_seconds=5.0,
            speaker_preview_min_segment=2.0,
        ),
        bus=bus or _FakeBus(),
    )


def _state(tmp_path: Path, dirs: dict[str, Path], options: dict) -> SimpleNamespace:
    import logging
    import uuid

    return SimpleNamespace(
        task_id=uuid.uuid4(),
        user_id="user",
        dirs=dirs,
        logger=logging.getLogger("test"),
        task_options=options,
    )


async def test_step_skipped_when_diarize_disabled(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": False}))

    assert backend.calls == []
    assert not (dirs["outputs"] / "diarization.json").exists()


async def test_step_writes_diarization_json(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    payload = json.loads((dirs["outputs"] / "diarization.json").read_text(encoding="utf-8"))
    assert payload["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    # Embeddings ship even though this task never reads them: pyannote returns
    # them for free, and vts-80i would otherwise re-process the whole audio.
    assert payload["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}
    assert payload["num_speakers"] == 1


async def test_step_diarizes_trimmed_audio_when_present(tmp_path: Path) -> None:
    # TrimInitialSilenceStep deletes audio_16k.wav, so the trimmed file is the
    # only one left — diarizing the missing original would crash the task.
    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k_trimmed.wav")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert backend.calls == [dirs["media"] / "audio_16k_trimmed.wav"]


async def test_step_already_done_when_artifact_exists(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["outputs"] / "diarization.json").write_text("{}", encoding="utf-8")
    backend = _FakeBackend()

    done = await DiarizeStep().already_done(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert done is True


async def test_step_already_done_false_when_enabled_and_missing(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    backend = _FakeBackend()

    done = await DiarizeStep().already_done(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert done is False


async def test_step_raises_when_no_segments_returned(tmp_path: Path) -> None:
    # A broken sidecar degrades to {"segments": [], ...}. Writing that would
    # render flat text — indistinguishable from a real monologue.
    class _EmptyBackend:
        async def diarize(self, audio_path: Path, timeout_seconds: int = 1800, **_kw) -> dict:
            return {"segments": [], "embeddings": {}, "num_speakers": 0}

    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    ctx = _ctx(_EmptyBackend())

    with pytest.raises(RuntimeError, match="no speaker segments"):
        await DiarizeStep().run(ctx, _state(tmp_path, dirs, {"diarize": True}))

    assert not (dirs["outputs"] / "diarization.json").exists()


def test_select_spans_spreads_across_segments() -> None:
    segments = [
        {"start": 0.0, "end": 30.0, "speaker": "S0"},   # long
        {"start": 40.0, "end": 45.0, "speaker": "S0"},  # 5s
        {"start": 50.0, "end": 51.0, "speaker": "S0"},  # too short (<2)
        {"start": 60.0, "end": 68.0, "speaker": "S0"},  # 8s
    ]
    spans = select_preview_spans(segments, "S0", count=3, clip_seconds=5.0, min_segment=2.0)
    # Three distinct source segments, longest first, each clip <= 5s, cut from inside.
    assert len(spans) == 3
    starts = [round(s["start"], 1) for s in spans]
    assert len(set(starts)) == 3  # distinct segments
    for s in spans:
        assert (s["end"] - s["start"]) <= 5.0 + 1e-6
        assert s["end"] - s["start"] >= 2.0  # nothing shorter than min survives as a clip


def test_select_spans_falls_back_to_one_segment_when_scarce() -> None:
    segments = [{"start": 0.0, "end": 30.0, "speaker": "S0"}]
    spans = select_preview_spans(segments, "S0", count=3, clip_seconds=5.0, min_segment=2.0)
    # Only one usable segment: take multiple non-overlapping clips from it.
    assert len(spans) == 3
    intervals = sorted((s["start"], s["end"]) for s in spans)
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert s2 >= e1  # non-overlapping


def test_diarize_is_in_the_dag_between_transcription_and_merge() -> None:
    # STEP_REGISTRY only maps names to instances; DAG_HEAD is what a task runs.
    # Without this the step is registered, tested, and never invoked.
    from vts.pipeline.types import DAG_HEAD

    assert "diarize" in DAG_HEAD
    assert DAG_HEAD.index("transcribe_segments") < DAG_HEAD.index("diarize")
    assert DAG_HEAD.index("diarize") < DAG_HEAD.index("merge_transcript")


def test_diarize_resolves_from_the_registry() -> None:
    from vts.pipeline.steps.registry import resolve_step

    assert isinstance(resolve_step("diarize"), DiarizeStep)


async def test_step_passes_task_id_as_job_id(tmp_path: Path) -> None:
    """The task id becomes the job id so a restart can re-attach."""
    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    backend = _FakeBackend()
    st = _state(tmp_path, dirs, {"diarize": True})

    await DiarizeStep().run(_ctx(backend), st)

    assert backend.job_ids == [str(st.task_id)]


async def test_step_publishes_progress(tmp_path: Path) -> None:
    """Progress from the sidecar reaches the bus as diarize_progress."""
    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    backend = _FakeBackend()
    bus = _FakeBus()

    await DiarizeStep().run(_ctx(backend, bus), _state(tmp_path, dirs, {"diarize": True}))

    progress = [e for e in bus.events if e.get("event") == "diarize_progress"]
    assert progress, "no diarize_progress event published"
    assert progress[0]["data"] == {"step": "embeddings", "completed": 1, "total": 2}
    assert progress[0]["throttle_key"] == "diarize_progress"


async def test_step_cancels_sidecar_when_task_cancelled(tmp_path: Path) -> None:
    """A cancel mid-diarization tells the sidecar to stop and exits quietly.

    This is vts-hv7: the processor only checks cancellation between steps, so
    without this the sidecar grinds on for the rest of its run after the user
    has discarded the task.
    """
    from vts.pipeline.steps.diarization import DiarizationCancelled

    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()
    bus = _FakeBus(cancel=True)
    st = _state(tmp_path, dirs, {"diarize": True})

    with pytest.raises(DiarizationCancelled):
        await DiarizeStep().run(_ctx(backend, bus), st)

    assert backend.cancelled == [str(st.task_id)]
    assert not (dirs["outputs"] / "diarization.json").exists()


async def test_step_cancels_sidecar_when_task_paused(tmp_path: Path) -> None:
    """A pause mid-diarization must stop the sidecar too, and stay a pause.

    The pause interrupt reaches the task through atask.cancel(), which unwinds
    the coroutine — but the diarization job lives in another process, so
    abandoning the await leaves it computing for the rest of its run. That is
    the same "pause does not free the hardware" bug this release fixes for the
    GPU, one step over.

    The exception matters as much as the cancel call: reusing
    DiarizationCancelled would make the processor exit quietly without writing
    a status, stranding the row in `running`. TaskPaused is the one that
    records `paused`.
    """
    from vts.pipeline.processor import TaskPaused

    dirs = _dirs(tmp_path)
    # A real wav, not a b"RIFF" stub: with the fix reverted the step runs past
    # the pause into preview clipping, and a stub would make it die on ffmpeg —
    # a failure that looks like proof but is about the fixture, not the pause.
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    backend = _FakeBackend()
    bus = _FakeBus(pause=True)
    st = _state(tmp_path, dirs, {"diarize": True})

    # Asserted as "the run stopped at the pause", not merely "the run raised":
    # without the fix the step sails past the pause and fails much later on
    # unrelated post-processing, which pytest.raises(Exception) would happily
    # accept. Only TaskPaused plus a cancelled sidecar job means the pause was
    # actually honoured.
    with pytest.raises(TaskPaused):
        await DiarizeStep().run(_ctx(backend, bus), st)

    assert backend.cancelled == [str(st.task_id)], "the sidecar job must be cancelled"
    assert not (dirs["outputs"] / "diarization.json").exists()


# --------------------------------------------------------------- RTF metrics
#
# The emitter branch was unreachable in every test above: `_ctx` builds a
# context without `get_emitter`, so the metric code added by b69a3e8 has never
# run under test. Both defects below shipped through that gap.

class _RecordingEmitter:
    """Collects emitted events. The real one writes JSONL synchronously."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(dict(event))


def _ctx_with_emitter(backend, emitter, bus=None) -> SimpleNamespace:
    ctx = _ctx(backend, bus)
    ctx.get_emitter = lambda _task_id: emitter
    return ctx


async def test_a_failed_run_is_not_recorded_as_ok(tmp_path: Path) -> None:
    """vts-i45s: the metric claimed success before the result was checked.

    The event was emitted, and written to disk synchronously, 17 lines above
    the guard that rejects an empty result. So a diarization that FAILED left
    `{"stage":"diarize.run","status":"ok","segments":0}` behind — with a
    plausible RTF, since a broken sidecar returns fast. Whoever later asks
    "what is our diarization RTF" reads exactly these lines.

    The timing is kept rather than dropped: a failed run's duration is real
    data. Only the status is honest about what happened.
    """
    class _EmptyBackend:
        async def diarize(self, audio_path: Path, timeout_seconds: int = 1800, **_kw) -> dict:
            return {"segments": [], "embeddings": {}, "num_speakers": 0}

    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    emitter = _RecordingEmitter()

    with pytest.raises(RuntimeError, match="no speaker segments"):
        await DiarizeStep().run(
            _ctx_with_emitter(_EmptyBackend(), emitter),
            _state(tmp_path, dirs, {"diarize": True}),
        )

    runs = [e for e in emitter.events if e.get("stage") == "diarize.run"]
    assert len(runs) == 1, f"expected one diarize.run event, got {emitter.events}"
    assert runs[0]["status"] == "error"
    assert runs[0]["segments"] == 0
    assert runs[0]["speakers"] == 0
    # The measurement survives: this row is still usable as "how long a failed
    # run took", which is why the event is not simply suppressed.
    assert isinstance(runs[0]["t_wall_ms"], int)


async def test_a_successful_run_is_recorded_as_ok(tmp_path: Path) -> None:
    """The counterpart, so "error" cannot be the answer to everything."""
    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    emitter = _RecordingEmitter()

    await DiarizeStep().run(
        _ctx_with_emitter(_FakeBackend(), emitter),
        _state(tmp_path, dirs, {"diarize": True}),
    )

    runs = [e for e in emitter.events if e.get("stage") == "diarize.run"]
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["segments"] == 1
    assert runs[0]["speakers"] == 1


async def test_the_duration_probe_leaves_the_event_loop_alive(
    tmp_path: Path, monkeypatch,
) -> None:
    """vts-p0mv: probe_duration ran synchronously inside an async step.

    It is `subprocess.run` — fork+exec of ffprobe — so for its whole duration
    the worker's event loop served nothing: not the progress reports of
    neighbouring tasks, not the heartbeat, not SSE. Everywhere else in the
    pipeline the same function is called through asyncio.to_thread
    (media.py:454, and _cut_wav in this very file).

    Proven without a sleep threshold: the fake probe waits on a threading
    event that only a COROUTINE can set. If the probe holds the loop, that
    coroutine never runs and the wait times out — which is the defect, stated
    as a deadlock rather than as a measured delay.
    """
    import asyncio
    import threading

    from vts.pipeline.steps import diarization as diarization_mod

    released = threading.Event()
    probed: list[Path] = []
    starved: list[str] = []

    def _fake_probe(path):
        probed.append(path)
        if not released.wait(timeout=5.0):
            # Recorded, not raised: the step wraps this call in `except
            # Exception` so a metric cannot fail it, and an AssertionError
            # would be swallowed there — leaving the real reason invisible.
            starved.append("loop never got a turn")
            return 0.0
        return 12.5

    monkeypatch.setattr(diarization_mod, "probe_duration", _fake_probe)

    dirs = _dirs(tmp_path)
    _write_silent_wav(dirs["media"] / "audio_16k.wav")
    emitter = _RecordingEmitter()

    async def _release() -> None:
        # Hand control back until the probe is running, then free it. A loop
        # that is blocked cannot reach this line.
        for _ in range(500):
            if probed:
                break
            await asyncio.sleep(0.01)
        released.set()

    await asyncio.gather(
        DiarizeStep().run(
            _ctx_with_emitter(_FakeBackend(), emitter),
            _state(tmp_path, dirs, {"diarize": True}),
        ),
        _release(),
    )

    assert probed, "probe_duration was never called"
    assert not starved, (
        "the event loop served nothing while probe_duration was in flight — "
        "the probe is running on the loop instead of in a thread"
    )
    runs = [e for e in emitter.events if e.get("stage") == "diarize.run"]
    assert runs[0]["audio_duration_s"] == 12.5, (
        "the probed duration did not reach the metric"
    )
