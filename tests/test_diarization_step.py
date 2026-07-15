import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.diarization import DiarizeStep


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def diarize(self, audio_path: Path, timeout_seconds: int = 1800) -> dict:
        self.calls.append(audio_path)
        return {
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [0.1, 0.2]},
            "num_speakers": 1,
        }


def _dirs(tmp_path: Path) -> dict[str, Path]:
    for name in ("media", "outputs", "segments", "logs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return {name: tmp_path / name for name in ("media", "outputs", "segments", "logs")}


def _ctx(backend: _FakeBackend) -> SimpleNamespace:
    def transcribe_audio_path(dirs: dict[str, Path]) -> Path:
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        return trimmed if trimmed.exists() else dirs["media"] / "audio_16k.wav"

    return SimpleNamespace(
        diarization=backend,
        transcribe_audio_path=transcribe_audio_path,
        settings=SimpleNamespace(diarization_enabled_default=False),
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
        async def diarize(self, audio_path: Path, timeout_seconds: int = 1800) -> dict:
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
