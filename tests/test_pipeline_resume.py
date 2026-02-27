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
