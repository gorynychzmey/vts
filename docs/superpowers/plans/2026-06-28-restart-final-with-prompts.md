# Restart Final With Prompts (vts-2or) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user restart a task's final stage with a *different* set of prompts — reusing the processed transcript (windows+pack), regenerating the whole finalize tail for the chosen set, and discarding removed prompts' results.

**Architecture:** Extend the existing `POST /api/tasks/restart_summary` with an optional `prompts` field honored only for `mode="final_only"`. When present, the server swaps `task.options.prompts` to the new set, deletes removed prompts' result files + `prompt_results` entries, clears ALL finalize artifacts, rebuilds the finalize-tail `steps` rows (update/insert/delete) to match the new set, and re-queues. The dynamic DAG (`build_dag_steps`) + client `getEnabledSteps` already adapt to `options.prompts`, so the worker and progress bar follow automatically. The task-card "Restart final summary only" menu item opens a dialog with a reusable prompt multiselect prefilled from the task's current set.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async + Postgres (tests via in-test Postgres), pytest; vanilla JS frontend.

**Spec:** [docs/superpowers/specs/2026-06-28-restart-final-with-prompts-design.md](../specs/2026-06-28-restart-final-with-prompts-design.md)

## Global Constraints

- Version bump: set `__version__` in `vts/__init__.py` before committing (current after merge — check; do a patch bump for the feature).
- `PromptRef` shape is fixed: `{"source": "system"|"user", "id": str}`. Reuse `vts.api.schemas.PromptRef` and `vts.services.prompt_registry.parse_ref`.
- Finalize step naming (server, authoritative): `finalize_step_name(source, id)` returns `"summarize_final"` for `(system, summary)` else `f"finalize:{source}:{id}"` (`vts/pipeline/types.py`).
- Head steps preserved on restart = `DAG_HEAD` (`vts/pipeline/types.py`): `download, extract_audio, trim_initial_silence, segment_audio, detect_language, transcribe_segments, merge_transcript, prepare_llama_model, prepare_summary_chunks, summarize_windows, pack_window_notes`.
- `steps` has `UniqueConstraint(task_id, name)` — rebuild via update-existing + insert-missing + delete-removed, never blind insert.
- JSON write-back: mutate a copy and reassign `task.options` (use `Repo.set_task_prompt_results` pattern); in-place dict mutation on the JSON column is not persisted.
- Result files: system/summary → `summary/final.md` (+`.json`) and `task.summary_path`; custom → `summary/results/{source}__{id}.md` (+`.json`).
- Tests run against real Postgres: `VTS_TEST_DATABASE_URL=postgresql+asyncpg://vts:vts@localhost:5432/vts_test`. Start a throwaway PG (`podman run -d --rm --name pg -e POSTGRES_USER=vts -e POSTGRES_PASSWORD=vts -e POSTGRES_DB=vts_test -p 5432:5432 postgres:16`, wait `pg_isready`), set the env var, run pytest, remove the container.
- Use the authed-client harness in `tests/conftest.py` (`client` fixture) for API tests; the `session` fixture in `tests/test_prompts_repo.py` for repo tests.

---

## File Structure

**Modified:**
- `vts/api/schemas.py` — add `prompts` to `RestartSummaryRequest`.
- `vts/api/main.py` — `restart_summary_tasks` endpoint: handle `prompts`; loosen `can_restart_final_summary_task`; add a finalize-tail rebuild helper + a result-clearing helper.
- `vts/db/repo.py` — add `delete_steps_by_name(task_id, names)`.
- `vts/services/prompt_results.py` — add `clear_all_finalize_results(task)` (delete files + reset index), reused by the endpoint.
- `vts/static/index.html` — restart-final dialog markup.
- `vts/static/app.js` — extract reusable multiselect render; wire "Restart final" → dialog; submit with `prompts`.
- `vts/static/styles.css`, `vts/static/i18n/{en,ru,de}.js` — dialog styles + keys.

**New tests:**
- `tests/test_restart_final_prompts.py` (API + reset behavior), plus repo-level additions in `tests/test_prompts_repo.py`.

---

## Task 1: Repo — delete steps by name

**Files:**
- Modify: `vts/db/repo.py` (after `upsert_step`, ~line 224)
- Test: `tests/test_prompts_repo.py` (extend)

**Interfaces:**
- Produces: `async Repo.delete_steps_by_name(task_id: uuid.UUID, names: list[str]) -> int` — deletes `steps` rows for the task whose `name` is in `names`; returns count deleted. No-op for empty `names`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_repo.py (append)
import uuid
import pytest
from vts.db.repo import Repo
from vts.db.models import User, Task, TaskStatus, Step, StepStatus


@pytest.mark.asyncio
async def test_delete_steps_by_name(session):
    repo = Repo(session)
    uid = uuid.uuid4()
    session.add(User(id=uid, username=f"u-{uid.hex[:8]}"))
    task = Task(id=uuid.uuid4(), user_id=uid, source_url="x", options={}, artifact_dir="/tmp/a")
    session.add(task)
    await session.flush()
    for name in ("summarize_final", "finalize:user:a", "summarize_windows"):
        session.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
    await session.flush()

    deleted = await repo.delete_steps_by_name(task.id, ["finalize:user:a", "summarize_final"])
    assert deleted == 2
    remaining = {s.name for s in (await repo.get_task_by_id(task.id)).steps}
    assert remaining == {"summarize_windows"}
    # empty names -> no-op
    assert await repo.delete_steps_by_name(task.id, []) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `VTS_TEST_DATABASE_URL=postgresql+asyncpg://vts:vts@localhost:5432/vts_test /home/victor/dev/vts/.venv/bin/python -m pytest tests/test_prompts_repo.py::test_delete_steps_by_name -v`
Expected: FAIL `AttributeError: 'Repo' object has no attribute 'delete_steps_by_name'`

- [ ] **Step 3: Write minimal implementation**

In `vts/db/repo.py` (uses already-imported `select`, `Step`; add `delete` from sqlalchemy if not present — check imports):

```python
    async def delete_steps_by_name(self, task_id: uuid.UUID, names: list[str]) -> int:
        if not names:
            return 0
        stmt = select(Step).where(Step.task_id == task_id, Step.name.in_(names))
        rows = list(await self.session.scalars(stmt))
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        return len(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2. Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_prompts_repo.py
git commit -m "feat(restart): repo delete_steps_by_name (vts-2or)"
```

---

## Task 2: prompt_results — clear all finalize results

**Files:**
- Modify: `vts/services/prompt_results.py`
- Test: `tests/test_prompts_repo.py` (extend; pure-function test, no DB needed but keep in this file)

**Interfaces:**
- Consumes: `result_entries`, `ref_key`.
- Produces:
  - `finalize_result_files(task) -> list[Path]` — all on-disk result files implied by the current `prompt_results` + `summary_path` (for deletion).
  - `clear_all_finalize_results(task) -> None` — deletes every finalize result file on disk (custom `summary/results/*.md|json`, system `summary/final.md|json` + `outputs/summary.md|json`), empties `task.options["prompt_results"]` (reassign), and sets `task.summary_path = None`. Mutates `task.options` via reassignment so the caller commits a persisted change.

Note: this clears ALL finalize results (the spec's "целиком" — even kept prompts are recomputed). The endpoint sets the new `options.prompts` separately (Task 4).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_repo.py (append)
from pathlib import Path
from types import SimpleNamespace
from vts.services.prompt_results import clear_all_finalize_results


def test_clear_all_finalize_results(tmp_path):
    summary = tmp_path / "summary"; (summary / "results").mkdir(parents=True)
    outputs = tmp_path / "outputs"; outputs.mkdir()
    (summary / "final.md").write_text("s")
    (summary / "results" / "user__a.md").write_text("a")
    (outputs / "summary.md").write_text("s")
    task = SimpleNamespace(
        artifact_dir=str(tmp_path),
        summary_path=str(summary / "final.md"),
        options={"prompts": [{"source": "user", "id": "a"}],
                 "prompt_results": [{"source": "user", "id": "a", "name": "A",
                                     "path": str(summary / "results" / "user__a.md"),
                                     "status": "completed"}]},
    )
    clear_all_finalize_results(task)
    assert not (summary / "final.md").exists()
    assert not (summary / "results" / "user__a.md").exists()
    assert not (outputs / "summary.md").exists()
    assert task.options["prompt_results"] == []
    assert task.summary_path is None
    # options.prompts is left untouched here (endpoint owns it)
    assert task.options["prompts"] == [{"source": "user", "id": "a"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_prompts_repo.py::test_clear_all_finalize_results -v`
Expected: FAIL `ImportError: cannot import name 'clear_all_finalize_results'`

- [ ] **Step 3: Write minimal implementation**

Append to `vts/services/prompt_results.py` (add `from pathlib import Path` at top):

```python
def clear_all_finalize_results(task) -> None:
    """Delete every finalize result file and reset the prompt_results index.

    Removes custom result files (summary/results/*), the system summary
    (summary/final.* + outputs/summary.*), empties options['prompt_results'],
    and clears task.summary_path. Reassigns task.options so the JSON column
    persists on commit.
    """
    artifact_root = Path(task.artifact_dir) if task.artifact_dir else None
    if artifact_root is not None:
        summary_dir = artifact_root / "summary"
        outputs_dir = artifact_root / "outputs"
        # custom result files
        results_dir = summary_dir / "results"
        if results_dir.exists():
            for p in results_dir.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        # system summary files
        for p in (summary_dir / "final.md", summary_dir / "final.json",
                  outputs_dir / "summary.md", outputs_dir / "summary.json"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    new_options = dict(task.options or {})
    new_options["prompt_results"] = []
    task.options = new_options
    task.summary_path = None
```

- [ ] **Step 4: Run test to verify it passes** — same command, Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/services/prompt_results.py tests/test_prompts_repo.py
git commit -m "feat(restart): clear_all_finalize_results helper (vts-2or)"
```

---

## Task 3: Schema — RestartSummaryRequest.prompts

**Files:**
- Modify: `vts/api/schemas.py` (`RestartSummaryRequest`, ~line 148)
- Test: `tests/test_restart_final_prompts.py` (new; schema-level)

**Interfaces:**
- Consumes: `PromptRef`.
- Produces: `RestartSummaryRequest` gains `prompts: list[PromptRef] | None = None`. Validator: if `prompts is not None` then `mode` must be `"final_only"` AND `prompts` must be non-empty, else `ValueError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_restart_final_prompts.py
import pytest
from pydantic import ValidationError
from vts.api.schemas import RestartSummaryRequest, PromptRef
import uuid

TID = [uuid.uuid4()]

def test_prompts_allowed_with_final_only():
    req = RestartSummaryRequest(task_ids=TID, mode="final_only",
                                prompts=[PromptRef(source="system", id="summary")])
    assert req.prompts == [PromptRef(source="system", id="summary")]

def test_prompts_rejected_with_full():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="full",
                              prompts=[PromptRef(source="system", id="summary")])

def test_empty_prompts_rejected():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="final_only", prompts=[])

def test_none_prompts_ok_any_mode():
    assert RestartSummaryRequest(task_ids=TID, mode="full").prompts is None
    assert RestartSummaryRequest(task_ids=TID, mode="final_only").prompts is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_restart_final_prompts.py -v`
Expected: FAIL (`prompts` not a field / no validator)

- [ ] **Step 3: Write minimal implementation**

In `vts/api/schemas.py`, replace `RestartSummaryRequest`:

```python
class RestartSummaryRequest(BaseModel):
    task_ids: list[UUID] = Field(min_length=1, max_length=100)
    mode: Literal["full", "final_only"] = "full"
    prompts: list[PromptRef] | None = None

    @model_validator(mode="after")
    def _validate_prompts(self) -> "RestartSummaryRequest":
        if self.prompts is not None:
            if self.mode != "final_only":
                raise ValueError("prompts is only allowed with mode=final_only")
            if len(self.prompts) == 0:
                raise ValueError("prompts must not be empty")
        return self
```

(`PromptRef`, `model_validator`, `Literal`, `Field` are already imported in this module — verify.)

- [ ] **Step 4: Run test to verify it passes** — same command, Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/api/schemas.py tests/test_restart_final_prompts.py
git commit -m "feat(restart): RestartSummaryRequest.prompts + validation (vts-2or)"
```

---

## Task 4: Endpoint — restart final with a new prompt set

**Files:**
- Modify: `vts/api/main.py` — `can_restart_final_summary_task` (~line 108), `restart_summary_tasks` (~line 1463), add a rebuild helper.
- Test: `tests/test_restart_final_prompts.py` (extend with API tests)

**Interfaces:**
- Consumes: `Repo.delete_steps_by_name`, `Repo.upsert_step`, `Repo.set_task_prompt_results`, `clear_all_finalize_results`, `build_dag_steps`, `DAG_HEAD`, `parse_ref`/`ref_to_dict`, `selected_prompt_refs`.
- Produces: endpoint behavior — `final_only` + `prompts` rebuilds the finalize tail for the new set and re-queues. New module-level helper:
  - `async def _rebuild_finalize_tail(repo, task, new_options) -> None` — given the task and the already-updated `new_options` (with new `prompts`), compute `target_tail = [s for s in build_dag_steps(new_options) if s not in DAG_HEAD]`; current finalize step rows = task steps whose name == "summarize_final" or startswith "finalize:"; `delete_steps_by_name` for current finalize names NOT in target_tail; for each name in target_tail, `upsert_step` then force it to pending (status=pending, attempt=0, started_at/finished_at/message=None).

**Loosen the gate** `can_restart_final_summary_task`: drop the "summary_selected" requirement; keep "summarize_windows completed" + task completed/failed-with-failed-final. New body:

```python
def can_restart_final_summary_task(task: Task) -> bool:
    summarize_windows_status = _find_step_status(task, "summarize_windows")
    if summarize_windows_status != StepStatus.completed:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return _find_step_status(task, "summarize_final") == StepStatus.failed
```

- [ ] **Step 1: Write the failing test** (API, through the `client` harness)

```python
# tests/test_restart_final_prompts.py (append)
import pytest

@pytest.mark.asyncio
async def test_restart_final_with_new_prompts_rebuilds_tail(client, authed_app, tmp_path, monkeypatch):
    """A completed task with [summary, user:a] restarted final with [summary, user:b]:
    options.prompts becomes the new set, prompt_results cleared, finalize steps
    rebuilt (finalize:user:a deleted, summarize_final + finalize:user:b pending),
    head steps untouched, status queued."""
    app, factory = authed_app
    from vts.db.models import User, Task, TaskStatus, Step, StepStatus, Prompt
    import uuid
    # Seed a user prompt 'b' so the ref is valid, a task with completed head + finals.
    async with factory() as s:
        from vts.api.main import _TEST_USER_ID  # if not exported, reuse conftest _TEST_USER_ID
        uid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
        pb = Prompt(id=uuid.uuid4(), user_id=uid, name="B", system_prompt="b")
        s.add(pb)
        art = tmp_path / "task"; (art / "summary").mkdir(parents=True)
        (art / "summary" / "final.md").write_text("old")
        task = Task(id=uuid.uuid4(), user_id=uid, source_url="x",
                    artifact_dir=str(art), status=TaskStatus.completed,
                    summary_path=str(art/"summary"/"final.md"),
                    options={"prompts": [{"source":"system","id":"summary"},
                                         {"source":"user","id":"a"}],
                             "prompt_results": [
                                {"source":"system","id":"summary","name":"S","path":str(art/"summary"/"final.md"),"status":"completed"},
                                {"source":"user","id":"a","name":"A","path":str(art/"summary"/"results"/"user__a.md"),"status":"completed"}]})
        s.add(task)
        for name in ["download","extract_audio","trim_initial_silence","segment_audio",
                     "detect_language","transcribe_segments","merge_transcript",
                     "prepare_llama_model","prepare_summary_chunks","summarize_windows",
                     "pack_window_notes","summarize_final","finalize:user:a"]:
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        task_id = str(task.id); pb_id = str(pb.id)

    resp = await client.post("/api/tasks/restart_summary", json={
        "task_ids": [task_id], "mode": "final_only",
        "prompts": [{"source":"system","id":"summary"}, {"source":"user","id":pb_id}],
    })
    assert resp.status_code == 200
    assert resp.json()["results"][task_id] == "queued"

    async with factory() as s:
        from vts.db.repo import Repo
        t = await Repo(s).get_task_by_id(uuid.UUID(task_id))
        assert t.status == TaskStatus.queued
        assert {tuple(p.values()) for p in t.options["prompts"]} == {
            ("system","summary"), ("user", pb_id)}
        step_status = {st.name: st.status for st in t.steps}
        assert "finalize:user:a" not in step_status                 # removed
        assert step_status[f"finalize:user:{pb_id}"] == StepStatus.pending  # added
        assert step_status["summarize_final"] == StepStatus.pending         # reset
        assert step_status["summarize_windows"] == StepStatus.completed     # head untouched
        assert t.summary_path is None
        assert t.options["prompt_results"] == []
```

(If `_TEST_USER_ID` isn't importable from main, hardcode the uuid as shown; it matches the conftest fake user.)

Also add:
```python
@pytest.mark.asyncio
async def test_restart_final_empty_prompts_422(client):
    resp = await client.post("/api/tasks/restart_summary",
        json={"task_ids":[str(uuid.uuid4())],"mode":"final_only","prompts":[]})
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_restart_final_prompts_with_full_422(client):
    resp = await client.post("/api/tasks/restart_summary",
        json={"task_ids":[str(uuid.uuid4())],"mode":"full",
              "prompts":[{"source":"system","id":"summary"}]})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run (with Postgres up + env var): `… -m pytest tests/test_restart_final_prompts.py -v`
Expected: FAIL — endpoint ignores `prompts`; tail not rebuilt.

- [ ] **Step 3: Write minimal implementation**

Add the `_rebuild_finalize_tail` helper near the other reset helpers in `main.py`:

```python
async def _rebuild_finalize_tail(repo: Repo, task: Task, new_options: dict) -> None:
    from vts.pipeline.types import DAG_HEAD, build_dag_steps
    target_tail = [s for s in build_dag_steps(new_options) if s not in DAG_HEAD]
    current_final = [
        st.name for st in task.steps
        if st.name == "summarize_final" or st.name.startswith("finalize:")
    ]
    to_delete = [n for n in current_final if n not in target_tail]
    await repo.delete_steps_by_name(task.id, to_delete)
    for name in target_tail:
        step = await repo.upsert_step(task.id, name)
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None
    await repo.session.flush()
```

In `restart_summary_tasks`, replace the `final_only` branch to honor `prompts`:

```python
            if request.mode == "final_only":
                if not can_restart_final_summary_task(task):
                    results[tid] = f"cannot_restart_final:{task.status.value}"
                    continue
                if request.prompts is not None:
                    # New-set restart: swap options.prompts, clear all finalize
                    # results, rebuild the finalize tail, re-queue.
                    new_refs = [
                        {"source": p.source, "id": p.id} for p in request.prompts
                    ]
                    clear_all_finalize_results(task)          # deletes files, empties prompt_results, summary_path=None
                    new_options = dict(task.options or {})
                    new_options["prompts"] = new_refs
                    task.options = new_options
                    await _rebuild_finalize_tail(repo, task, new_options)
                else:
                    _reset_final_summary_step(task)
                    artifact_resets.append(asyncio.to_thread(_reset_final_summary_artifacts, task))
            else:
                ...
```

Note: when `prompts` is given, `clear_all_finalize_results` already set `summary_path=None` and reset `prompt_results`; the shared `task.summary_path = None` line below is harmless. Keep `set_task_summary_progress(task, 0, 0)` and `set_task_status(task, queued)`.

Replace `can_restart_final_summary_task` with the loosened version above.

- [ ] **Step 4: Run to verify it passes** — same command, Expected: PASS (all 3+ tests)

- [ ] **Step 5: Update the gate test for the new semantics**

`tests/test_task_transitions.py::test_can_restart_final_summary_task` has a `no_summary` case (`options={"summary": False}, steps=[]`) asserting `not can_restart_final_summary_task(...)`. With the loosened gate this STILL passes — but only because `steps=[]` means `summarize_windows` is not completed (the windows path, not the summary path). To prove the NEW semantics, add a case: a task with NO summary in its set but WITH `summarize_windows` completed and status completed now returns `True`:

```python
    # tests/test_task_transitions.py — add inside test_can_restart_final_summary_task
    custom_only = SimpleNamespace(
        status=TaskStatus.completed,
        options={"prompts": [{"source": "user", "id": "a"}]},
        steps=[windows_ok],  # windows done, no summarize_final present
    )
    assert can_restart_final_summary_task(custom_only)  # NEW: gate no longer requires summary
```

Leave the existing `no_summary` assertion as-is (it remains correct via the windows-not-done path), but add a comment that it now passes for that reason.

- [ ] **Step 6: Run the full suite (Postgres) for regressions**

Run: `… -m pytest -q`
Expected: PASS (the updated `test_task_transitions.py` plus all existing tests).

- [ ] **Step 6: Commit**

```bash
git add vts/api/main.py tests/test_restart_final_prompts.py tests/test_task_transitions.py
git commit -m "feat(restart): final restart with a new prompt set (vts-2or)"
```

---

## Task 5: Frontend — extract reusable multiselect

**Files:**
- Modify: `vts/static/app.js` (`renderPromptSelect` ~1754, `getSelectedPrompts` ~1832, `setPromptPopoverOpen` ~1708)
- Test: manual (`node --check`) + reasoning.

**Interfaces:**
- Produces: a reusable renderer `renderPromptMultiselect(container, prompts, selectedRefs)` that builds the toggle+popover into `container`, checking the boxes whose `{source,id}` are in `selectedRefs`; and `getSelectedFrom(container) -> [{source,id}]`. The existing task-form selector becomes a caller of these (passing `#prompt-select` and the default `[{system,summary}]`), so its behavior is unchanged.

This task is a refactor with no behavior change to the create form. Keep `getSelectedPrompts()` working (delegate to `getSelectedFrom(promptSelect)`), keep `syncSummaryToggle` and `resetPromptSelection` working.

- [ ] **Step 1: Refactor render into a container-parameterized function**

Extract the body of `renderPromptSelect` into `renderPromptMultiselect(container, prompts, selectedRefs)`: same DOM construction, but (a) operate on `container` not the global `promptSelect`; (b) a checkbox is checked iff `selectedRefs.some(r => r.source===prompt.source && r.id===prompt.id)`; (c) the toggle/summary/popover/outside-click wiring keys off `container` (the outside-click + Escape handlers must close the popover of whichever container is open — generalize the existing document-level handlers to act on any `.prompt-select.open`).

Then:
```js
function getSelectedFrom(container) {
  return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
    .map(cb => ({ source: cb.dataset.source, id: cb.dataset.id }));
}
function getSelectedPrompts() {           // create-form caller, unchanged contract
  return promptSelect ? getSelectedFrom(promptSelect) : [];
}
```
`renderPromptSelect(prompts)` becomes: `renderPromptMultiselect(promptSelect, prompts, [{source:"system",id:"summary"}]); syncSummaryToggle();`.

- [ ] **Step 2: Generalize outside-click/Escape close**

The document-level handlers (currently `app.js:2407` click, `2419` keydown) must close ANY open `.prompt-select` popover, not only `#prompt-select`. Change to iterate `document.querySelectorAll(".prompt-select.open")` and close those whose container does not contain the click target (for click) / all (for Escape). Use the shared `setPromptPopoverOpen` made container-aware (`setPromptPopoverOpen(container, open)`).

- [ ] **Step 3: Verify no behavior change to the create form**

Run: `node --check vts/static/app.js` → OK.
Reason: the create-form selector still renders, defaults to system:summary, getSelectedPrompts unchanged.

- [ ] **Step 4: Commit**

```bash
git add vts/static/app.js
git commit -m "refactor(ui): reusable prompt multiselect (container-parameterized) (vts-2or)"
```

---

## Task 6: Frontend — restart-final dialog

**Files:**
- Modify: `vts/static/index.html` (add `<dialog id="restart-final-dialog">`), `vts/static/app.js` (wire "Restart final" → dialog), `vts/static/styles.css`, `vts/static/i18n/{en,ru,de}.js`
- Test: manual (`node --check`) + reasoning; full Python suite for no-regression.

**Interfaces:**
- Consumes: `renderPromptMultiselect`, `getSelectedFrom`, `restartSummary`-style POST.

- [ ] **Step 1: Add dialog markup** in `index.html` (near the prompts dialog, before the script tags):

```html
<dialog id="restart-final-dialog" class="tokens-dialog">
  <div class="tokens-dialog-header">
    <h2 data-i18n="restart_final.title">Restart final with prompts</h2>
    <button type="button" id="restart-final-close-btn" class="icon-btn ghost"
            data-i18n-title="tokens.close" aria-label="Close">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12"/><path d="M6 18L18 6"/></svg>
    </button>
  </div>
  <div class="prompt-select" id="restart-final-select"></div>
  <div class="prompt-form-actions">
    <button type="button" id="restart-final-submit-btn" class="btn-text primary"
            data-i18n="restart_final.submit">Restart</button>
  </div>
</dialog>
```

- [ ] **Step 2: Wire the menu item** — change the "Restart final summary only" handler (`app.js` ~1533, currently `restartSummary(task.id, "final_only")`) to open the dialog instead: load `/api/prompts`, `renderPromptMultiselect(restartFinalSelect, prompts, task.options.prompts || [{source:"system",id:"summary"}])`, store the target task id, `showModal()`. Submit handler reads `getSelectedFrom(restartFinalSelect)`, disables submit if empty, POSTs `/api/tasks/restart_summary` `{task_ids:[id], mode:"final_only", prompts}`, closes dialog, `loadTasks()`. Close button / Escape closes.

- [ ] **Step 3: i18n** — add `restart_final.title`, `restart_final.submit` in en/ru/de:
  - en: "Restart final with prompts", "Restart"
  - ru: "Перезапустить финал с промптами", "Перезапустить"
  - de: "Finale mit Prompts neu starten", "Neu starten"

- [ ] **Step 4: CSS** — reuse `.tokens-dialog`/`.prompt-form-actions`; ensure `#restart-final-select.prompt-select` gets `position: relative` so its popover anchors (the existing `.prompt-select` rule already does).

- [ ] **Step 5: Verify**

Run: `node --check vts/static/app.js`; `node --check` on the 3 i18n files. Then full Python suite (Postgres) `… -m pytest -q` — must stay green (no Python touched here, but confirm).

- [ ] **Step 6: Commit**

```bash
git add vts/static/ tests/
git commit -m "feat(ui): restart-final dialog with prompt selection (vts-2or)"
```

---

## Task 7: Version bump + CHANGELOG + full suite

**Files:**
- Modify: `vts/__init__.py`, `CHANGELOG.md`

- [ ] **Step 1: Bump version** in `vts/__init__.py` (patch bump from current).
- [ ] **Step 2: CHANGELOG entry** — under a new version heading: "Restart final stage with a different prompt set (`final_only` + `prompts` on `/api/tasks/restart_summary`); task-card 'Restart final' opens a prompt-selection dialog. Removed prompts' results are discarded."
- [ ] **Step 3: Full suite (Postgres)** `… -m pytest -q` → PASS.
- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py CHANGELOG.md
git commit -m "chore(restart): version bump + changelog (vts-2or)"
```

---

## Self-Review Notes

**Spec coverage:**
- Concept (reuse processed transcript, regen finalize tail wholesale, new-set replaces) → Tasks 2,4. ✓
- API: `prompts` only with `final_only` (422 otherwise), empty rejected → Task 3; behavior → Task 4. ✓
- Gate loosened to "summarize_windows completed" → Task 4. ✓
- Reset mechanics: swap options.prompts, delete removed results + clear all finalize artifacts, rebuild finalize-tail steps (update/insert/delete, unique-key safe), head untouched, requeue → Tasks 1,2,4. ✓
- UI: "Restart final" → dialog with prefilled reusable multiselect → Tasks 5,6. ✓
- Progress already covered (dynamic getEnabledSteps) → no task needed (noted). ✓
- Back-compat (`final_only` no prompts; `full`) → preserved in Task 4 branch; regression run Step 5. ✓
- Out of scope vts-a93 → not in plan. ✓

**Placeholder scan:** none — all steps carry concrete code/commands.

**Type consistency:** `delete_steps_by_name(task_id, names)->int`, `clear_all_finalize_results(task)`, `_rebuild_finalize_tail(repo, task, new_options)`, `renderPromptMultiselect(container, prompts, selectedRefs)`, `getSelectedFrom(container)` used consistently across tasks.

**Open implementation note (flagged, not a blocker):** Task 5's generalization of the document-level outside-click/Escape handlers must not break the create-form selector — the refactor keeps `#prompt-select` as one `.prompt-select.open` among possibly two. Reviewer should confirm both selectors coexist.
