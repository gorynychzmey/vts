import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.processor import TaskProcessor


class _DummyBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)


class _DummyHeavySlot:
    async def __aenter__(self) -> "_DummyHeavySlot":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _make_dirs(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "task"
    outputs = root / "outputs"
    summary = root / "summary"
    outputs.mkdir(parents=True, exist_ok=True)
    summary.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "outputs": outputs,
    }


def test_step_summarize_windows_resumes_from_partial_windows_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        prompts_dir=tmp_path / "prompts",
        llama_url="http://llama.local/v1",
        llama_model="Qwen2.5-7B-Instruct-Q4_K_M",
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    dirs = _make_dirs(tmp_path)
    summary_dir = dirs["root"] / "summary"
    windows_file = summary_dir / "windows.json"
    chunks_file = summary_dir / "chunks.json"

    chunks_file.write_text(
        json.dumps({"chunks": ["chunk one", "chunk two", "chunk three"]}),
        encoding="utf-8",
    )
    first_summary = {"topic": "already done", "bullets": ["a"], "action_items": []}
    windows_file.write_text(
        json.dumps(
            {
                "windows": [
                    {
                        "window_index": 1,
                        "summary": first_summary,
                        "path": str(summary_dir / "window_01.txt"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("vts.pipeline.processor.load_prompt", lambda *args, **kwargs: "segment prompt")

    calls: list[dict[str, object]] = []

    async def _fake_chat_completion(**kwargs: object) -> str:
        calls.append(kwargs)
        user_prompt = str(kwargs.get("user_prompt", ""))
        if "Window 2/" in user_prompt:
            return json.dumps({"topic": "second", "bullets": ["b"], "action_items": []})
        if "Window 3/" in user_prompt:
            return json.dumps({"topic": "third", "bullets": ["c"], "action_items": []})
        raise AssertionError(f"unexpected prompt: {user_prompt}")

    monkeypatch.setattr("vts.pipeline.processor.llama_chat_completion", _fake_chat_completion)

    success = asyncio.run(
        TaskProcessor.step_summarize_windows(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs=dirs,
            logger=logging.getLogger("test_step_summarize_windows_resume"),
            task_options={},
            dry_run=False,
        )
    )

    assert success is True
    assert len(calls) == 2
    assert all("Window 1/" not in str(call.get("user_prompt", "")) for call in calls)

    payload = json.loads(windows_file.read_text(encoding="utf-8"))
    windows = payload["windows"]
    assert [item["window_index"] for item in windows] == [1, 2, 3]
    assert windows[0]["summary"] == first_summary
    assert windows[1]["summary"]["topic"] == "second"
    assert windows[2]["summary"]["topic"] == "third"
    assert (dirs["outputs"] / "window_summaries.json").exists()
    assert len(processor.bus.events) == 3


def test_step_summarize_windows_dry_run_accepts_empty_windows(tmp_path: Path) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        prompts_dir=tmp_path / "prompts",
        llama_url="http://llama.local/v1",
        llama_model="Qwen2.5-7B-Instruct-Q4_K_M",
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    dirs = _make_dirs(tmp_path)
    summary_dir = dirs["root"] / "summary"
    (summary_dir / "windows.json").write_text(json.dumps({"windows": []}), encoding="utf-8")

    success = asyncio.run(
        TaskProcessor.step_summarize_windows(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs=dirs,
            logger=logging.getLogger("test_step_summarize_windows_dry_run_empty"),
            task_options={},
            dry_run=True,
        )
    )

    assert success is True


def test_step_detect_language_fallback_when_segments_are_missing_but_transcript_exists(
    tmp_path: Path,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        language_detection_confidence_threshold=0.6,
        whisper_url="http://whisper.local",
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    root = tmp_path / "task"
    outputs = root / "outputs"
    segments = root / "segments"
    outputs.mkdir(parents=True, exist_ok=True)
    segments.mkdir(parents=True, exist_ok=True)
    (outputs / "segments_manifest.json").write_text(
        json.dumps({"segments": [{"segment_index": 1, "file": "0001.wav"}]}),
        encoding="utf-8",
    )
    (outputs / "transcript.json").write_text(
        json.dumps({"text": "Это пример русского текста для теста."}),
        encoding="utf-8",
    )

    success = asyncio.run(
        TaskProcessor.step_detect_language(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs={"root": root, "outputs": outputs, "segments": segments},
            logger=logging.getLogger("test_step_detect_language_resume_fallback"),
            task_options={},
            dry_run=False,
        )
    )

    assert success is True
    marker = json.loads((outputs / "language_detection.json").read_text(encoding="utf-8"))
    assert marker["source"] == "resume_transcript_fallback"
    assert marker["language"] == "ru"


def test_step_detect_language_accepts_missing_confidence_when_language_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        language_detection_confidence_threshold=0.6,
        whisper_url="http://whisper.local",
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    async def _persist_detected_language(*args: object, **kwargs: object) -> None:
        return None

    processor._persist_detected_language = _persist_detected_language

    async def _fake_transcribe_with_whisper(**kwargs: object) -> dict[str, object]:
        return {"language": "ru"}

    monkeypatch.setattr("vts.pipeline.processor.transcribe_with_whisper", _fake_transcribe_with_whisper)

    root = tmp_path / "task"
    outputs = root / "outputs"
    segments = root / "segments"
    outputs.mkdir(parents=True, exist_ok=True)
    segments.mkdir(parents=True, exist_ok=True)
    (outputs / "segments_manifest.json").write_text(
        json.dumps({"segments": [{"segment_index": 1, "file": "0001.wav"}]}),
        encoding="utf-8",
    )
    (segments / "0001.wav").write_bytes(b"wav")

    task_options: dict[str, object] = {}
    success = asyncio.run(
        TaskProcessor.step_detect_language(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs={"root": root, "outputs": outputs, "segments": segments},
            logger=logging.getLogger("test_step_detect_language_missing_confidence"),
            task_options=task_options,
            dry_run=False,
        )
    )

    assert success is True
    assert task_options["detected_language"] == "ru"
    marker = json.loads((outputs / "language_detection.json").read_text(encoding="utf-8"))
    assert marker["source"] == "whisper_first_segment"
    assert marker["language"] == "ru"
    assert marker["confidence"] == 0.6
    assert marker["confidence_source"] == "assumed_threshold"


def test_step_segment_audio_publishes_progress_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        segment_search_window_seconds=30,
        segment_target_seconds=60,
        segment_overlap_seconds=5,
        db_write_throttle_ms=0,
    )
    processor.bus = _DummyBus()

    class _DummySession:
        async def __aenter__(self) -> "_DummySession":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def commit(self) -> None:
            return None

    processor.session_factory = lambda: _DummySession()

    class _DummyRepo:
        def __init__(self, session: object) -> None:
            self.session = session

        async def clear_asr_for_task(self, task_id: uuid.UUID) -> None:
            return None

        async def upsert_asr_segment_payload(
            self,
            task_id: uuid.UUID,
            segment_index: int,
            start_sec: float,
            end_sec: float,
            text: str,
            raw_json: dict[str, object],
        ) -> object:
            return object()

    monkeypatch.setattr("vts.pipeline.processor.Repo", _DummyRepo)
    monkeypatch.setattr("vts.pipeline.processor.probe_duration", lambda *args, **kwargs: 130.0)
    monkeypatch.setattr("vts.pipeline.processor.detect_silence_points", lambda *args, **kwargs: [60.0, 120.0])

    def _fake_export_segments(
        audio_wav: Path,
        segments: list[tuple[float, float]],
        segment_dir: Path,
        log_path: Path,
        progress_cb: object = None,
    ) -> list[dict[str, object]]:
        specs: list[dict[str, object]] = []
        total = len(segments)
        for idx, (start, end) in enumerate(segments, start=1):
            segment_file = segment_dir / f"{idx:04d}.wav"
            segment_file.parent.mkdir(parents=True, exist_ok=True)
            segment_file.write_bytes(b"wav")
            specs.append(
                {
                    "segment_index": idx,
                    "start": float(start),
                    "end": float(end),
                    "file": segment_file.name,
                }
            )
            if callable(progress_cb):
                progress_cb(idx, total)
        return specs

    monkeypatch.setattr("vts.pipeline.processor.export_segments", _fake_export_segments)

    root = tmp_path / "task"
    outputs = root / "outputs"
    segments = root / "segments"
    logs = root / "logs"
    media = root / "media"
    outputs.mkdir(parents=True, exist_ok=True)
    segments.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    (media / "audio_16k.wav").write_bytes(b"wav")

    success = asyncio.run(
        TaskProcessor.step_segment_audio(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs={"root": root, "outputs": outputs, "segments": segments, "logs": logs, "media": media},
            logger=logging.getLogger("test_step_segment_audio_progress"),
            task_options={},
            dry_run=False,
        )
    )

    assert success is True
    progress_events = [event for event in processor.bus.events if event.get("event") == "segment_progress"]
    assert progress_events
    assert progress_events[0]["data"] == {"current": 0, "total": 3}
    assert progress_events[-1]["data"] == {"current": 3, "total": 3}
    phase_events = [event for event in processor.bus.events if event.get("event") == "phase"]
    assert phase_events[-1]["data"] == {"phase": "segment_audio", "segments": 3}
    manifest = json.loads((outputs / "segments_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest.get("segments", [])) == 3
