import asyncio
import functools
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.processor import TaskProcessor
from vts.services.prompt_registry import list_system_prompts
from vts.services.prompt_results import (
    result_entries,
    resolve_result_path,
    upsert_result_entry,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
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


class _FakeLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def get_n_ctx(self) -> int:
        return 32768

    async def count_tokens(self, **kwargs: object) -> int:
        return 100

    async def chat_completion(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.output


class _StubTask:
    """In-memory stand-in for a Task row with a plain JSON `options` column."""

    def __init__(self, options: dict) -> None:
        self.options = options
        self.summary_path: str | None = None
        self.summary_progress: dict | None = None
        self.updated_at = None


class _StubPrompt:
    def __init__(self, id: uuid.UUID, user_id: uuid.UUID, name: str, system_prompt: str) -> None:
        self.id = id
        self.user_id = user_id
        self.name = name
        self.system_prompt = system_prompt


class _StubSession:
    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


def _make_processor(tmp_path: Path, monkeypatch, *, llm_output: str, task, prompt=None):
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.settings = SimpleNamespace(
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
    proc.bus = _DummyBus()
    proc.heavy_slot = _DummyHeavySlot()
    proc.logger = logging.getLogger("test_finalize_loop")
    proc._task_metrics = {}  # so _get_emitter returns None (skip metrics block)
    proc._log_payload = lambda *a, **k: None
    proc._effective_language = lambda *a, **k: "en"
    proc._render_prompt_with_language = lambda prompt, language: prompt
    proc._llm = _FakeLLM(llm_output)
    monkeypatch.setattr("vts.pipeline.processor.load_prompt", lambda *a, **k: "SYSTEM PROMPT")

    proc.session_factory = lambda: _StubSession()

    class _StubRepo:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get_task_by_id(self, task_id: uuid.UUID):
            return task

        async def set_task_summary_progress(self, t, current: int, total: int) -> None:
            t.summary_progress = {"current": current, "total": total}

        async def get_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID):
            if prompt is not None and prompt.id == prompt_id and prompt.user_id == user_id:
                return prompt
            return None

        async def set_task_prompt_results(self, t, prompt_results: list[dict]) -> None:
            new_options = dict(t.options or {})
            new_options["prompt_results"] = prompt_results
            t.options = new_options

    monkeypatch.setattr("vts.pipeline.processor.Repo", _StubRepo)
    return proc


def _write_packed_notes(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "task"
    outputs = root / "outputs"
    summary = root / "summary"
    outputs.mkdir(parents=True, exist_ok=True)
    summary.mkdir(parents=True, exist_ok=True)
    (summary / "packed_notes.json").write_text(
        json.dumps({"notes": ["note one", "note two"], "packing_triggered": False}),
        encoding="utf-8",
    )
    return {"root": root, "outputs": outputs}


# --------------------------------------------------------------------------- #
# Unit tests
# --------------------------------------------------------------------------- #
def test_upsert_result_entry_inserts_then_updates() -> None:
    options: dict = {}
    entries = upsert_result_entry(options, "user", "abc", "My Prompt", "/p/a.md", "completed")
    assert entries == [
        {"source": "user", "id": "abc", "name": "My Prompt", "path": "/p/a.md", "status": "completed"}
    ]
    # second upsert for same ref updates in place (no duplicate)
    entries = upsert_result_entry(options, "user", "abc", "Renamed", "/p/b.md", "completed")
    assert len(entries) == 1
    assert entries[0]["name"] == "Renamed"
    assert entries[0]["path"] == "/p/b.md"


def test_upsert_result_entry_does_not_alias_input_list() -> None:
    """The returned list must NOT be the same object as options['prompt_results'],
    and the input's existing list must not be mutated in place.

    Regression (vts-jal): _persist_prompt_result passes a SHALLOW dict(task.options),
    so options['prompt_results'] is the same list SQLAlchemy loaded for the JSON
    column. If upsert mutates that list in place, the change is not tracked and the
    second finalize step's write is silently dropped on commit -> only one
    prompt_result survives -> results dropdown stays hidden.
    """
    original_list = [
        {"source": "system", "id": "summary", "name": "S", "path": "/p/s.md", "status": "completed"}
    ]
    options = {"prompt_results": original_list}
    entries = upsert_result_entry(options, "user", "abc", "U", "/p/u.md", "completed")
    # New entry is present in the returned list...
    assert len(entries) == 2
    assert {(e["source"], e["id"]) for e in entries} == {("system", "summary"), ("user", "abc")}
    # ...but the caller's original list object was NOT mutated in place.
    assert original_list == [
        {"source": "system", "id": "summary", "name": "S", "path": "/p/s.md", "status": "completed"}
    ]
    assert entries is not original_list


def test_resolve_prompt_text_system_uses_registry(tmp_path: Path, monkeypatch) -> None:
    task = _StubTask({})
    proc = _make_processor(tmp_path, monkeypatch, llm_output="x", task=task)
    text = asyncio.run(proc.resolve_prompt_text("system", "summary", "en", str(uuid.uuid4())))
    assert text == "SYSTEM PROMPT"


def test_resolve_prompt_text_user_loads_from_db(tmp_path: Path, monkeypatch) -> None:
    uid = uuid.uuid4()
    pid = uuid.uuid4()
    prompt = _StubPrompt(pid, uid, "Custom", "DO THE THING ${LANG}")
    task = _StubTask({})
    proc = _make_processor(tmp_path, monkeypatch, llm_output="x", task=task, prompt=prompt)
    text = asyncio.run(proc.resolve_prompt_text("user", str(pid), "en", str(uid)))
    assert text == "DO THE THING ${LANG}"


def test_resolve_prompt_text_user_missing_raises(tmp_path: Path, monkeypatch) -> None:
    task = _StubTask({})
    proc = _make_processor(tmp_path, monkeypatch, llm_output="x", task=task, prompt=None)
    with pytest.raises(RuntimeError, match="user prompt not found"):
        asyncio.run(
            proc.resolve_prompt_text("user", str(uuid.uuid4()), "en", str(uuid.uuid4()))
        )


# --------------------------------------------------------------------------- #
# End-to-end finalize tests
# --------------------------------------------------------------------------- #
def test_finalize_writes_result_index_for_custom_prompt(tmp_path: Path, monkeypatch) -> None:
    uid = uuid.uuid4()
    pid = uuid.uuid4()
    prompt = _StubPrompt(pid, uid, "My Custom Prompt", "Summarise differently.")
    options = {"prompts": [{"source": "user", "id": str(pid)}]}
    task = _StubTask(options)
    proc = _make_processor(
        tmp_path, monkeypatch, llm_output="CUSTOM RESULT", task=task, prompt=prompt
    )
    dirs = _write_packed_notes(tmp_path)
    task_id = uuid.uuid4()

    ok = asyncio.run(
        proc.step_finalize_prompt(
            task_id,
            str(uid),
            dirs,
            proc.logger,
            options,
            dry_run=False,
            source="user",
            id=str(pid),
        )
    )
    assert ok is True

    # Result file written under summary/results/, NOT final.md
    result_md = dirs["root"] / "summary" / "results" / f"user__{pid}.md"
    assert result_md.exists()
    assert result_md.read_text(encoding="utf-8") == "CUSTOM RESULT"
    assert not (dirs["root"] / "summary" / "final.md").exists()
    # Custom prompts must not clobber the canonical summary back-compat outputs.
    assert not (dirs["outputs"] / "summary.md").exists()
    assert task.summary_path is None

    entries = result_entries(task)
    assert any(
        e["source"] == "user"
        and e["id"] == str(pid)
        and e["status"] == "completed"
        and e["name"] == "My Custom Prompt"
        for e in entries
    )
    assert resolve_result_path(task, "user", str(pid)) == str(result_md)


def test_finalize_system_summary_keeps_backcompat(tmp_path: Path, monkeypatch) -> None:
    task = _StubTask({})
    proc = _make_processor(tmp_path, monkeypatch, llm_output="THE SUMMARY", task=task)
    dirs = _write_packed_notes(tmp_path)
    task_id = uuid.uuid4()

    ok = asyncio.run(
        proc.step_finalize_prompt(
            task_id,
            str(uuid.uuid4()),
            dirs,
            proc.logger,
            {},
            dry_run=False,
            source="system",
            id="summary",
        )
    )
    assert ok is True

    final_md = dirs["root"] / "summary" / "final.md"
    assert final_md.exists()
    assert final_md.read_text(encoding="utf-8") == "THE SUMMARY"
    assert (dirs["outputs"] / "summary.md").read_text(encoding="utf-8") == "THE SUMMARY"
    assert task.summary_path == str(final_md)

    # The summary is also tracked in the result index, with its i18n name key.
    sysdef = next(p for p in list_system_prompts() if p.key == "summary")
    entries = result_entries(task)
    assert any(
        e["source"] == "system" and e["id"] == "summary" and e["name"] == sysdef.i18n_name_key
        for e in entries
    )


def test_finalize_rejects_traversal_user_id(tmp_path: Path, monkeypatch) -> None:
    """A user-source id that is not a UUID is rejected before any path is built,
    and no file is written outside the results dir."""
    task = _StubTask({"prompts": [{"source": "user", "id": "../../etc/passwd"}]})
    proc = _make_processor(tmp_path, monkeypatch, llm_output="X", task=task)
    dirs = _write_packed_notes(tmp_path)

    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(
            proc.step_finalize_prompt(
                uuid.uuid4(),
                str(uuid.uuid4()),
                dirs,
                proc.logger,
                task.options,
                dry_run=False,
                source="user",
                id="../../etc/passwd",
            )
        )
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    # No new file created anywhere (in or out of the results dir).
    assert after == before
    # And nothing escaped above the task tree.
    assert not (tmp_path.parent / "passwd").exists()


def test_dispatch_routes_summarize_final_and_custom(monkeypatch) -> None:
    """summarize_final and finalize:<src>:<id> both bind step_finalize_prompt
    with the right source/id (mirrors _run_step dispatch logic)."""
    from vts.services.prompt_registry import parse_ref

    captured: list[tuple[str, str]] = []

    class _P:
        async def step_finalize_prompt(self, *a, source, id, **k):
            captured.append((source, id))
            return True

    proc = _P()

    def _bind(step_name: str):
        if step_name == "summarize_final":
            return functools.partial(proc.step_finalize_prompt, source="system", id="summary")
        if step_name.startswith("finalize:"):
            s, i = parse_ref(step_name.split(":", 1)[1])
            return functools.partial(proc.step_finalize_prompt, source=s, id=i)
        raise AssertionError("not a finalize step")

    asyncio.run(_bind("summarize_final")(uuid.uuid4(), "u", {}, None, {}, dry_run=False))
    asyncio.run(_bind("finalize:user:abc")(uuid.uuid4(), "u", {}, None, {}, dry_run=False))
    assert captured == [("system", "summary"), ("user", "abc")]
