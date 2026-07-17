import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.diarization import DiarizeStep


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

    def __init__(self, cancel: bool = False) -> None:
        self.events: list[dict] = []
        self._cancel = cancel

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    async def is_cancel_requested(self, task_id) -> bool:
        return self._cancel


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
        settings=SimpleNamespace(diarization_enabled_default=False),
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
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
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
    (dirs["media"] / "audio_16k_trimmed.wav").write_bytes(b"RIFF")
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
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()
    st = _state(tmp_path, dirs, {"diarize": True})

    await DiarizeStep().run(_ctx(backend), st)

    assert backend.job_ids == [str(st.task_id)]


async def test_step_publishes_progress(tmp_path: Path) -> None:
    """Progress from the sidecar reaches the bus as diarize_progress."""
    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
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
