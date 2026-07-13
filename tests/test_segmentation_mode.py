"""Skip-segmentation feature (vts-o51): summary.segmentation = always|never|auto.

prepare_summary_chunks decides whole-vs-split from the context window;
summarize_windows honors the whole mode (long timeout) and falls back to
segmentation on a context-overflow error in auto mode, or fails explicitly
in never mode.

Token counting in fakes: 1 token per whitespace-separated word.
"""
from __future__ import annotations

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


class _FakeLLM:
    """Word-count tokenizer; scripted chat responses; recording chunker."""

    def __init__(self, chat_results: list[object] | None = None, split_into: int = 2) -> None:
        self.chat_results = list(chat_results or [])
        self.split_into = split_into
        self.chat_calls: list[dict[str, object]] = []
        self.chunk_calls: list[dict[str, object]] = []

    async def count_tokens(self, *, text: str, **kwargs: object) -> int:
        return len(text.split())

    async def chunk_text(self, *, text: str, window_tokens: int, **kwargs: object) -> list[str]:
        self.chunk_calls.append({"window_tokens": window_tokens, "text": text})
        words = text.split()
        n = max(1, self.split_into)
        size = max(1, (len(words) + n - 1) // n)
        return [" ".join(words[i : i + size]) for i in range(0, len(words), size)]

    async def chat_completion(self, **kwargs: object) -> str:
        self.chat_calls.append(kwargs)
        if not self.chat_results:
            return "REWRITTEN"
        result = self.chat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return str(result)


def _make_dirs(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "task"
    outputs = root / "outputs"
    (root / "summary").mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    return {"root": root, "outputs": outputs}


def _make_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    n_ctx: int,
    segmentation: str = "auto",
    fake_llm: _FakeLLM | None = None,
) -> tuple[TaskProcessor, _FakeLLM]:
    processor = TaskProcessor.__new__(TaskProcessor)
    processor.settings = SimpleNamespace(
        prompts_dir=tmp_path / "prompts",
        llm_url="http://llm.local/v1",
        llm_model="test-model",
        llm_api_key=None,
        llm_temperature=0.2,
        llm_top_p=None,
        llm_min_p=None,
        llm_repeat_penalty=None,
        llm_thinking=None,
        llm_tokenizer_path=None,
        llm_chat_timeout_seconds=600,
        llm_final_timeout_seconds=1800,
        summary_segmentation=segmentation,
        summary_segment_window_cap=8192,
        summary_n_ctx=n_ctx,
    )
    processor.bus = _DummyBus()
    processor.heavy_slot = _DummyHeavySlot()
    processor._task_metrics = {}
    processor._task_n_ctx = {}
    processor._log_payload = lambda *a, **k: None
    processor._effective_language = lambda *a, **k: "en"
    processor._render_prompt_with_language = lambda prompt, language: prompt

    async def _noop_progress(*a: object, **k: object) -> None:
        return None

    processor._persist_summary_progress = _noop_progress
    llm = fake_llm or _FakeLLM()
    processor._llm = llm

    async def _stub_discover_n_ctx(**kwargs: object) -> tuple[str, int]:
        return ("llama-server", n_ctx)

    monkeypatch.setattr("vts.pipeline.processor.discover_n_ctx", _stub_discover_n_ctx)
    monkeypatch.setattr(
        "vts.pipeline.processor.load_prompt",
        lambda *a, **k: "SEGMENT PROMPT " + " ".join(["p"] * 98),  # 100 tokens
    )
    return processor, llm


def _write_transcript(dirs: dict[str, Path], words: int) -> str:
    text = " ".join(f"w{i}" for i in range(words))
    (dirs["outputs"] / "transcript.json").write_text(
        json.dumps({"text": text}), encoding="utf-8"
    )
    return text


def _run_prepare(processor: TaskProcessor, dirs: dict[str, Path]) -> bool:
    return asyncio.run(
        TaskProcessor.step_prepare_summary_chunks(
            processor,
            task_id=uuid.uuid4(),
            user_id="u1",
            dirs=dirs,
            logger=logging.getLogger("test_segmentation"),
            task_options={},
            dry_run=False,
        )
    )


def _run_windows(processor: TaskProcessor, dirs: dict[str, Path]) -> bool:
    return asyncio.run(
        TaskProcessor.step_summarize_windows(
            processor,
            task_id=uuid.uuid4(),
            user_id="u1",
            dirs=dirs,
            logger=logging.getLogger("test_segmentation"),
            task_options={},
            dry_run=False,
        )
    )


def _chunks_payload(dirs: dict[str, Path]) -> dict:
    return json.loads((dirs["root"] / "summary" / "chunks.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# prepare_summary_chunks decision
# ---------------------------------------------------------------------------


def test_prepare_auto_fitting_transcript_goes_whole(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    text = _write_transcript(dirs, words=500)  # 100 + 2*500 + 768 <= 5000
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000)

    assert _run_prepare(processor, dirs) is True
    payload = _chunks_payload(dirs)
    assert payload["segmentation"] == "whole"
    assert payload["chunks"] == [text]
    assert llm.chunk_calls == []  # no splitting happened


def test_prepare_auto_oversized_transcript_splits_with_derived_window(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_transcript(dirs, words=60000)  # 100 + 120000 + 768 > 114688
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=114688)

    assert _run_prepare(processor, dirs) is True
    payload = _chunks_payload(dirs)
    assert payload["segmentation"] == "split"
    assert len(payload["chunks"]) == 2
    # (114688 - 100 - 768) / 2 = 56910 -> capped at 8192
    assert llm.chunk_calls[0]["window_tokens"] == 8192


def test_prepare_always_splits_even_when_fitting(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_transcript(dirs, words=500)
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000, segmentation="always")

    assert _run_prepare(processor, dirs) is True
    payload = _chunks_payload(dirs)
    assert payload["segmentation"] == "split"
    assert len(llm.chunk_calls) == 1


def test_prepare_never_impossible_transcript_fails_before_llm(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    # hard check (min_ratio 0.30 default): 100 + 1300*1.3 + 768 = 2558 > 2000
    _write_transcript(dirs, words=1300)
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=2000, segmentation="never")

    with pytest.raises(RuntimeError, match="never"):
        _run_prepare(processor, dirs)
    assert llm.chat_calls == []
    assert llm.chunk_calls == []
    assert not (dirs["root"] / "summary" / "chunks.json").exists()


def test_prepare_never_goes_whole_even_when_conservative_check_fails(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    # conservative: 100 + 2000 + 768 = 2868 > 2500 -> auto would split;
    # hard: 100 + 1000*1.3 + 768 = 2168 <= 2500 -> never sends whole
    text = _write_transcript(dirs, words=1000)
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=2500, segmentation="never")

    assert _run_prepare(processor, dirs) is True
    payload = _chunks_payload(dirs)
    assert payload["segmentation"] == "whole"
    assert payload["chunks"] == [text]


# ---------------------------------------------------------------------------
# summarize_windows: whole mode behavior
# ---------------------------------------------------------------------------


def _write_chunks(dirs: dict[str, Path], chunks: list[str], segmentation: str) -> None:
    (dirs["root"] / "summary" / "chunks.json").write_text(
        json.dumps({"chunks": chunks, "segmentation": segmentation}), encoding="utf-8"
    )


def test_windows_whole_mode_uses_final_timeout(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_chunks(dirs, [" ".join(["w"] * 300)], "whole")
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000)

    assert _run_windows(processor, dirs) is True
    assert len(llm.chat_calls) == 1
    assert llm.chat_calls[0]["timeout_seconds"] == 1800
    windows = json.loads((dirs["root"] / "summary" / "windows.json").read_text())["windows"]
    assert len(windows) == 1


def test_windows_split_mode_keeps_chat_timeout(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_chunks(dirs, ["chunk one", "chunk two"], "split")
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000)

    assert _run_windows(processor, dirs) is True
    assert [c["timeout_seconds"] for c in llm.chat_calls] == [600, 600]


def test_windows_whole_overflow_auto_falls_back_to_split(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    text = " ".join(f"w{i}" for i in range(400))
    _write_chunks(dirs, [text], "whole")
    overflow = RuntimeError(
        "llama chat completion failed with HTTP 400 for http://x/v1/chat/completions: "
        "the request exceeds the available context size"
    )
    llm = _FakeLLM(chat_results=[overflow, "PART ONE", "PART TWO"])
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000, fake_llm=llm)

    assert _run_windows(processor, dirs) is True
    payload = _chunks_payload(dirs)
    assert payload["segmentation"] == "split"
    assert len(payload["chunks"]) == 2
    windows = json.loads((dirs["root"] / "summary" / "windows.json").read_text())["windows"]
    assert [w["summary"] for w in windows] == ["PART ONE", "PART TWO"]
    # progress reset announced with the new total (2 windows + final = 3)
    resets = [
        e for e in processor.bus.events
        if e.get("event") == "summary_progress" and e.get("data", {}).get("current") == 0
    ]
    assert any(e["data"]["total"] == 3 for e in resets)
    redacted = (dirs["outputs"] / "redacted_transcript.txt").read_text()
    assert "PART ONE" in redacted and "PART TWO" in redacted


def test_windows_whole_overflow_never_raises_explicit_error(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_chunks(dirs, [" ".join(["w"] * 400)], "whole")
    overflow = RuntimeError("This model's maximum context length is 5000 tokens")
    llm = _FakeLLM(chat_results=[overflow])
    processor, llm = _make_processor(
        tmp_path, monkeypatch, n_ctx=5000, segmentation="never", fake_llm=llm
    )

    with pytest.raises(RuntimeError, match="one piece"):
        _run_windows(processor, dirs)
    assert llm.chunk_calls == []  # no fallback in never mode


def test_windows_non_overflow_error_propagates_unchanged(tmp_path, monkeypatch):
    dirs = _make_dirs(tmp_path)
    _write_chunks(dirs, [" ".join(["w"] * 400)], "whole")
    llm = _FakeLLM(chat_results=[RuntimeError("connection refused")])
    processor, llm = _make_processor(tmp_path, monkeypatch, n_ctx=5000, fake_llm=llm)

    with pytest.raises(RuntimeError, match="connection refused"):
        _run_windows(processor, dirs)
    assert llm.chunk_calls == []
