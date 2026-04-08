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

    async def is_pause_requested(self, task_id: object) -> bool:
        return False


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
        llm_url="http://llama.local/v1",
        llm_model="Qwen2.5-7B-Instruct-Q4_K_M",
        llm_temperature=0.2,
        llm_top_p=None,
        llm_min_p=None,
        llm_repeat_penalty=None,
        llm_thinking=None,
        llm_api_key=None,
        llm_tokenizer_path=None,
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    async def _noop_persist_summary_progress(*args: object, **kwargs: object) -> None:
        return None

    processor._persist_summary_progress = _noop_persist_summary_progress

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

    class _FakeLLM:
        async def get_n_ctx(self) -> int:
            return 32768

        async def count_tokens(self, **kwargs: object) -> int:
            return 500

        async def chat_completion(self, **kwargs: object) -> str:
            calls.append(kwargs)
            assert kwargs.get("use_json_format") is False, "segment calls must not use JSON format"
            user_prompt = str(kwargs.get("user_prompt", ""))
            if "Window 2/" in user_prompt:
                return "## Topics\n- second\n\n## Facts and Examples\n- b"
            if "Window 3/" in user_prompt:
                return "## Topics\n- third\n\n## Facts and Examples\n- c"
            raise AssertionError(f"unexpected prompt: {user_prompt}")

    processor._llm = _FakeLLM()

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
    assert isinstance(windows[1]["summary"], str) and "second" in windows[1]["summary"]
    assert isinstance(windows[2]["summary"], str) and "third" in windows[2]["summary"]
    assert (dirs["outputs"] / "window_summaries.json").exists()
    # 1 summary_progress for already-skipped window 1
    # + 2 × (segment_summary_text + summary_progress) for windows 2 and 3 = 5
    assert len(processor.bus.events) == 5


def test_step_summarize_windows_dry_run_accepts_empty_windows(tmp_path: Path) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        prompts_dir=tmp_path / "prompts",
        llm_url="http://llama.local/v1",
        llm_model="Qwen2.5-7B-Instruct-Q4_K_M",
        llm_temperature=0.2,
        llm_top_p=None,
        llm_min_p=None,
        llm_repeat_penalty=None,
        llm_thinking=None,
        llm_api_key=None,
        llm_tokenizer_path=None,
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


def test_step_extract_audio_dry_run_accepts_trimmed_output(tmp_path: Path) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace()
    processor.bus = _DummyBus()

    root = tmp_path / "task"
    media = root / "media"
    logs = root / "logs"
    media.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    (media / "audio_16k_trimmed.wav").write_bytes(b"wav")

    success = asyncio.run(
        TaskProcessor.step_extract_audio(
            processor,
            task_id=uuid.uuid4(),
            user_id="user-1",
            dirs={"media": media, "logs": logs},
            logger=logging.getLogger("test_step_extract_audio_trimmed_resume"),
            task_options={},
            dry_run=True,
        )
    )

    assert success is True


def test_step_detect_language_raises_when_first_segment_missing(
    tmp_path: Path,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        language_detection_confidence_threshold=0.6,
        whisper_url="http://whisper.local",
        whisper_backend="asr",
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
    # segment file is missing — no fallback, should raise

    with pytest.raises(RuntimeError, match="Missing first segment"):
        asyncio.run(
            TaskProcessor.step_detect_language(
                processor,
                task_id=uuid.uuid4(),
                user_id="user-1",
                dirs={"root": root, "outputs": outputs, "segments": segments},
                logger=logging.getLogger("test_step_detect_language_missing_segment"),
                task_options={},
                dry_run=False,
            )
        )


def test_step_detect_language_raises_when_confidence_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        language_detection_confidence_threshold=0.6,
        whisper_url="http://whisper.local",
        whisper_backend="asr",
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None

    class _FakeWhisper:
        async def detect_language(self, **kwargs: object) -> dict[str, object]:
            return {"language": "ru"}  # no language_probability

    processor.whisper = _FakeWhisper()  # type: ignore[assignment]

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

    with pytest.raises(RuntimeError, match="language_probability missing"):
        asyncio.run(
            TaskProcessor.step_detect_language(
                processor,
                task_id=uuid.uuid4(),
                user_id="user-1",
                dirs={"root": root, "outputs": outputs, "segments": segments},
                logger=logging.getLogger("test_step_detect_language_missing_confidence"),
                task_options={},
                dry_run=False,
            )
        )


def test_step_segment_audio_publishes_progress_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        segment_search_window_seconds=30,
        segment_target_seconds=60,
        segment_overlap_seconds=5,
        services_database_write_throttle_ms=0,
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


def _make_processor_for_final_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
    """Return (processor, dummy_task) wired up for step_summarize_final tests."""
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        prompts_dir=tmp_path / "prompts",
        llm_url="http://llama.local/v1",
        llm_model="Qwen2.5-7B-Instruct-Q4_K_M",
        llm_temperature=0.2,
        llm_top_p=None,
        llm_min_p=None,
        llm_repeat_penalty=None,
        llm_thinking=None,
        llm_api_key=None,
        llm_tokenizer_path=None,
        llm_final_timeout_seconds=120,
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._log_payload = lambda *args, **kwargs: None
    processor._effective_language = lambda *args, **kwargs: "en"
    processor._render_prompt_with_language = lambda prompt, language: prompt
    monkeypatch.setattr("vts.pipeline.processor.load_prompt", lambda *args, **kwargs: "prompt")

    class _FakeLLM:
        async def get_n_ctx(self) -> int:
            return 32768

        async def count_tokens(self, **kwargs: object) -> int:
            return 100

        async def chat_completion(self, **kwargs: object) -> str:
            raise AssertionError("chat_completion not expected in this test")

    processor._llm = _FakeLLM()

    class _DummySession:
        async def __aenter__(self) -> "_DummySession":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def commit(self) -> None:
            return None

    class _DummyTask:
        def __init__(self) -> None:
            self.summary_path: str | None = None

    dummy_task = _DummyTask()
    processor.session_factory = lambda: _DummySession()

    class _DummyRepo:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_task_by_id(self, task_id: uuid.UUID) -> _DummyTask:
            return dummy_task

        async def set_task_summary_progress(self, task: object, current: int, total: int) -> None:
            return None

    monkeypatch.setattr("vts.pipeline.processor.Repo", _DummyRepo)
    return processor, dummy_task
