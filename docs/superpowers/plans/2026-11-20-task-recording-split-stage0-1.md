# Task/Recording split — stages 0 and 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A recording exists for every task from creation, carries its own completeness state, receives every content write, and the library shows only recordings that have text.

**Architecture:** Two releases. Stage 0 adds `recordings.state`, keeps it in sync from the one place task statuses change (`Repo.set_task_status*`), and filters the library. Stage 1 creates the recording inside `Repo.create_task`, adds the recording's own content columns, routes every content writer through a small set of repo setters that write the recording AND (until stage 3) the old task fields, fixes donor clones, and backfills.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / Alembic / Postgres (pgvector) / vanilla JS frontend / Playwright UI scenarios.

**Spec:** `docs/superpowers/specs/2026-11-20-task-recording-split-design.md` (stages 2–3 get their own plan once stage 1 runs on prod).

## Global Constraints

- Tests run on Postgres, never SQLite: `.venv/bin/python -m pytest` (the worktree's own venv; no `uv`). UI: full suite `node tests/ui/run.mjs`; one scenario `node tests/ui/scenario-child.mjs tests/ui/scenarios/<name>.mjs`.
- JSON columns are reassigned, never mutated in place.
- Every commit that ships code bumps `vts/__init__.py` (patch for fixes, minor for features). One minor bump per release is enough: bump in the first code commit of a release, patch-bump in later commits of the same release only if a tag already exists for the current number. Check `git tag -l "build-<version>"` before each bump.
- Commit narrowly: `git add <paths>`, never `-A`/`-a`; no tree-wide `stash`/`checkout -- .`. Another agent works on `main`.
- Build tags (`build-X.Y.Z`) only with Victor's explicit, single-use permission.
- Public repo: no private hostnames, paths or keys in code, tests, comments.
- Code comments, docstrings, commit messages in English; reference the bd id `vts-lzad`.
- `artifacts_root` is a container path; migrations that read files follow the `0031_source_timeline` pattern (logic in `vts/services/…`, skip cleanly when the data volume is not mounted).

## Review Focus

1. **Task deleted while the pipeline runs.** `delete_task_with_recording` removes the recording too; every new setter must treat "no recording" as a no-op, never raise. Test in Task 6.
2. **Restart of a completed task.** Summary restart / resume puts the task back to `queued`; the recording must flip to `processing` and stay visible (it has text). Test in Task 2.
3. **Archiving never changes state.** `set_task_status(task, archived)` from `failed` must leave `incomplete`. Test in Task 2.
4. **Library count agrees with the list** once rows without text are hidden (`count_recordings` uses the same filter). Test in Task 3.
5. **Legacy detached recordings** (`source_task_id IS NULL`) with a transcript stay visible and keep a state after the backfill. Test in Task 1.

---

## Stage 0 — release A

### Task 1: `recordings.state` column, enum, backfill migration

**Files:**
- Modify: `vts/db/models.py` (add `RecordingState`, `Recording.state`)
- Create: `vts/services/recording_state.py`
- Create: `alembic/versions/0032_recording_state.py`
- Test: `tests/test_recording_state.py`

**Interfaces:**
- Produces: `class RecordingState(str, enum.Enum): processing = "processing"; complete = "complete"; incomplete = "incomplete"`; column `Recording.state: Mapped[RecordingState]`, `Enum(RecordingState, name="recording_state", native_enum=False)`, `nullable=False`, `default=RecordingState.processing`, `server_default="processing"`.
- Produces: `def state_for_status(status: TaskStatus) -> RecordingState | None` — `completed→complete`; `failed, canceled→incomplete`; `queued, running→processing`; `paused, waiting, awaiting_input, archived→None` (None = leave unchanged).
- Produces: `def initial_state(status: TaskStatus, *, transcript_path: str | None, summary_path: str | None, prompts_requested: bool) -> RecordingState` — `state_for_status(status)` when not None; for `archived`: `complete` if `summary_path` or (`not prompts_requested` and `transcript_path`), else `incomplete`; for `paused/waiting/awaiting_input`: `processing`.
- Produces: `def backfill_recording_states(conn) -> int` (sync Connection, used by the migration): sets `state` for every recording from its task via `initial_state`; recordings with `source_task_id IS NULL` → `complete` if `transcript_path` else `incomplete`. `prompts_requested` = task `options["prompts"]` is a non-empty list.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("status,expected", [
    (TaskStatus.completed, RecordingState.complete),
    (TaskStatus.failed, RecordingState.incomplete),
    (TaskStatus.canceled, RecordingState.incomplete),
    (TaskStatus.queued, RecordingState.processing),
    (TaskStatus.running, RecordingState.processing),
    (TaskStatus.paused, None), (TaskStatus.waiting, None),
    (TaskStatus.awaiting_input, None), (TaskStatus.archived, None),
])
def test_state_for_status(status, expected):
    assert state_for_status(status) is expected

def test_initial_state_archived_with_summary_is_complete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path="/s", prompts_requested=True) is RecordingState.complete

def test_initial_state_archived_without_summary_but_prompts_is_incomplete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path=None, prompts_requested=True) is RecordingState.incomplete

def test_initial_state_archived_transcript_only_task_is_complete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path=None, prompts_requested=False) is RecordingState.complete

async def test_backfill_sets_state_from_task_and_detached(factory):
    # rows: completed task, failed task, archived task w/o summary (prompts requested),
    # detached recording with transcript, detached recording without transcript
    ...
    assert states == {"completed": "complete", "failed": "incomplete",
                      "archived": "incomplete", "detached_text": "complete",
                      "detached_empty": "incomplete"}
```

Use the `factory` fixture pattern from `tests/test_recordings.py`; call `backfill_recording_states` via `await conn.run_sync(...)`.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_recording_state.py -v` — Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** the enum/column in `models.py`, the three functions in `recording_state.py`, and migration `0032_recording_state` (`down_revision = "0031_source_timeline"`): `op.add_column` with `server_default="processing"`, then `backfill_recording_states(op.get_bind())`; downgrade drops the column.
- [ ] **Step 4: Run** the test file — Expected: PASS. The migration itself is a thin wrapper around the tested function (as `0031` is); exercise it once with `alembic upgrade head` against the local test Postgres.
- [ ] **Step 5: Commit** (with the release's minor version bump in `vts/__init__.py`):
```bash
git add vts/db/models.py vts/services/recording_state.py alembic/versions/0032_recording_state.py tests/test_recording_state.py vts/__init__.py
git commit -m "feat(library): recordings carry their own completeness state (vts-lzad)"
```

### Task 2: keep state in sync from task status changes

**Files:**
- Modify: `vts/db/repo.py` (`set_task_status`, `set_task_status_by_id`, `upsert_recording_for_task`)
- Test: `tests/test_recording_state.py`

**Interfaces:**
- Consumes: `state_for_status`, `initial_state` (Task 1).
- Produces: `Repo._sync_recording_state(self, task_id: uuid.UUID, status: TaskStatus) -> None` — `UPDATE recordings SET state=… WHERE source_task_id=:task_id` when `state_for_status(status)` is not None; no-op otherwise and when no recording exists. Called at the end of `set_task_status` and `set_task_status_by_id`.
- Produces: `upsert_recording_for_task` sets `state = initial_state(task.status, …)` **only when creating** the recording.

All completed/failed/canceled/queued transitions in the codebase go through these two methods (checked: processor completion/failure, donor clone, cancel in API/MCP/worker, resume, summary restarts, speaker resolution). `transition_task_status` (running↔waiting) and `requeue_running_tasks` (→queued from running) need no hook: the recording is already `processing`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_completing_a_task_completes_its_recording(factory): ...
    await repo.set_task_status(task, TaskStatus.completed)
    assert recording.state is RecordingState.complete

async def test_failed_then_archived_stays_incomplete(factory): ...
    await repo.set_task_status(task, TaskStatus.failed)
    await repo.set_task_status(task, TaskStatus.archived)
    assert recording.state is RecordingState.incomplete

async def test_restart_puts_complete_recording_back_to_processing(factory): ...
    await repo.set_task_status(task, TaskStatus.completed)
    await repo.set_task_status(task, TaskStatus.queued)
    assert recording.state is RecordingState.processing

async def test_cancel_by_id_marks_incomplete(factory): ...
    await repo.set_task_status_by_id(task.id, TaskStatus.canceled)
    assert recording.state is RecordingState.incomplete

async def test_status_change_without_recording_is_a_noop(factory): ...
    await repo.set_task_status(task_without_recording, TaskStatus.completed)  # no raise

async def test_upsert_creating_for_completed_task_starts_complete(factory): ...
    task.status = TaskStatus.completed
    rec = await repo.upsert_recording_for_task(task)
    assert rec.state is RecordingState.complete
```

(Refresh the recording from the DB before asserting: the update is a bulk UPDATE.)

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** `_sync_recording_state` and the two call sites; the create branch in `upsert_recording_for_task`.
- [ ] **Step 4: Run** `tests/test_recording_state.py tests/test_recordings.py tests/test_pipeline_resume.py` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): follow task status into the recording's state (vts-lzad)`.

### Task 3: hide recordings without text; expose state in HTTP and MCP

**Files:**
- Modify: `vts/db/repo.py:578` (`_apply_recording_filters`)
- Modify: `vts/api/schemas.py` (`RecordingOut`), `vts/api/routers/recordings.py:47` (`_serialize`)
- Modify: `vts/mcp/schemas.py:64` (`RecordingInfo`), `vts/mcp/tools_registry/recordings.py:45` (`_info`)
- Modify: `docs/API.md` (recording fields)
- Test: `tests/test_recordings_filters.py`, `tests/test_recording_serialization.py`, `tests/mcp/test_recording_tools.py`

**Interfaces:**
- Produces: `_apply_recording_filters` always adds `Recording.transcript_path.isnot(None)`. `get_recording_for_user` stays unfiltered (a recording is still reachable by id through its task).
- Produces: `RecordingOut.state: Literal["processing", "complete", "incomplete"]`; `RecordingInfo.state` same.

- [ ] **Step 1: Write the failing tests**

```python
async def test_recording_without_transcript_is_not_listed_nor_counted(factory):
    # one recording with transcript_path, one without
    assert [r.id for r in await repo.list_recordings(_USER)] == [with_text.id]
    assert await repo.count_recordings(_USER) == 1

async def test_recording_without_transcript_is_still_fetchable_by_id(factory):
    assert await repo.get_recording_for_user(_USER, empty.id) is not None

def test_serialized_recording_carries_state():
    assert _serialize(rec_incomplete).state == "incomplete"

async def test_mcp_list_recordings_reports_state_and_hides_empty(...):
    page = await _list_recordings()
    assert [(i.id, i.state) for i in page.items] == [(with_text.id, "processing")]
```

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**; update `docs/API.md` recording object (add `state`, note that recordings without a transcript are not listed).
- [ ] **Step 4: Run** `tests/test_recordings*.py tests/test_recording_serialization.py tests/mcp/ -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): list only recordings that have text, with their state (vts-lzad)`.

### Task 4: library mark in the web UI

**Files:**
- Modify: `vts/static/app.js:8437` (`applyLibraryMeta`)
- Modify: `vts/static/i18n/en.js`, `ru.js`, `de.js`
- Create: `tests/ui/scenarios/library-recording-state.mjs`
- Modify (generated): `docs/ui-inventory.md` via `make ui-inventory`

**Interfaces:**
- Consumes: `RecordingOut.state` (Task 3).
- Produces: i18n keys `library.state_processing` = "processing" / "обрабатывается" / "wird verarbeitet"; `library.state_incomplete` = "incomplete" / "неполная" / "unvollständig". `applyLibraryMeta` appends the label after language and before `library.detached`; `complete` adds nothing.

- [ ] **Step 1: Write the failing scenario** modelled on `library-detached-recording.mjs`: stub `/api/recordings` with three items (`complete`, `processing`, `incomplete`); assert each card's `.library-meta` text — exact strings: complete card contains neither "processing" nor "incomplete"; processing card contains "processing"; incomplete card contains "incomplete".
- [ ] **Step 2: Run** the scenario alone — Expected: FAIL.
- [ ] **Step 3: Implement** the two lines in `applyLibraryMeta` and the six i18n entries; run `make ui-inventory`.
- [ ] **Step 4: Run** the scenario, then the full UI suite and `make ui-inventory-check` — Expected: PASS. Revert the `applyLibraryMeta` change once and confirm the scenario fails (prove it tests the symptom), then restore.
- [ ] **Step 5: Commit** `feat(library): mark recordings still processing or left incomplete (vts-lzad)`.

### Release A checkpoint

- [ ] Full backend suite `.venv/bin/python -m pytest -q` and UI suite green.
- [ ] Pre-tag checks from CLAUDE.md (`git status --short`, `git log --oneline -5`, version in file, `git tag -l`).
- [ ] Ask Victor for the build tag. After deploy: confirm on prod that the migration ran (`recordings.state` populated; no row left at the server default for a completed task) via the prod query wrapper.

---

## Stage 1 — release B (start only after release A is on prod)

### Task 5: recording content columns and repo setters

**Files:**
- Modify: `vts/db/models.py` (`Recording.redacted_path: Mapped[str | None]`, `Recording.prompt_results: Mapped[list[dict]]` JSON, `nullable=False`, `default=list`)
- Create: `alembic/versions/0033_recording_content.py` (schema only; `server_default` `'[]'::json`)
- Modify: `vts/db/repo.py` (new section "Recording content (vts-lzad)")
- Test: `tests/test_recording_content.py`

**Interfaces:**
- Produces: `UNSET` sentinel in `vts/db/repo.py`.
- Produces: `async def recording_for_task(self, task_id: uuid.UUID) -> Recording | None`.
- Produces: `async def set_task_content(self, task: Task, *, transcript_path: str | None = UNSET, summary_path: str | None = UNSET, redacted_path: str | None = UNSET) -> None` — writes each given field to the recording; also writes `task.transcript_path` / `task.summary_path` (dual-write until stage 3; the task has no redacted column). No recording → only the task side.
- Produces: `set_task_prompt_results(task, entries)` additionally sets `recording.prompt_results = [dict(e) for e in entries]` (reassigned).
- Produces: `async def set_detected_language(self, task: Task, language: str, confidence: float) -> None` — writes `options["detected_language"]`, `options["detected_language_confidence"]` (reassigned) and `recording.language = language_code(language)` when it normalises to a code.
- Produces: `async def mirror_prompt_results(self, task: Task) -> None` — copies `task.options["prompt_results"]` to the recording; for callers of the pure helpers in `vts/services/prompt_results.py`, which mutate `task.options` synchronously.

- [ ] **Step 1: Write the failing tests**

```python
async def test_set_task_content_writes_recording_and_task(factory):
    await repo.set_task_content(task, transcript_path="/a/outputs/transcript.txt", redacted_path="/a/outputs/redacted_transcript.txt")
    assert (task.transcript_path, rec.transcript_path, rec.redacted_path) == ("/a/outputs/transcript.txt",) * 2 + ("/a/outputs/redacted_transcript.txt",)

async def test_unset_fields_are_left_alone(factory):
    await repo.set_task_content(task, summary_path="/s")
    await repo.set_task_content(task, summary_path=None)
    assert rec.summary_path is None and rec.transcript_path == original

async def test_prompt_results_reach_the_recording(factory):
    entries = [{"source": "user", "id": "x", "name": "Memo", "path": "/p", "status": "completed"}]
    await repo.set_task_prompt_results(task, entries)
    assert rec.prompt_results == entries

async def test_detected_language_is_normalised_on_the_recording(factory):
    await repo.set_detected_language(task, "russian", 0.9)
    assert rec.language == "ru" and task.options["detected_language"] == "russian"

async def test_setters_without_recording_do_not_raise(factory): ...
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** (minor bump for release B) `feat(recordings): the recording holds its own content fields (vts-lzad)`.

### Task 6: create the recording with the task

**Files:**
- Modify: `vts/db/repo.py:95` (`create_task`)
- Test: `tests/test_recording_content.py`, plus one API-level test in `tests/test_recordings.py`

**Interfaces:**
- Produces: `create_task` adds `Recording(user_id, source_task_id=task.id, artifact_dir, title=recording_display_name(source_title, source_url), source_url, recorded_at=task.created_at, state=processing)` in the same flush. Covers `POST /api/tasks`, `POST /api/tasks/upload`, upload finalize (`uploads.py:593,619`), MCP `submit_video` (`mcp/tools.py:240`).
- `upsert_recording_for_task` keeps working unchanged (finds the row).

- [ ] **Step 1: Write the failing tests**

```python
async def test_create_task_creates_its_recording(factory):
    task = await repo.create_task(user_id=_USER, source_url="https://example.com/v", options={}, artifact_dir="/a")
    rec = await repo.recording_for_task(task.id)
    assert (rec.state, rec.artifact_dir, rec.transcript_path) == (RecordingState.processing, "/a", None)

async def test_new_recording_is_not_in_the_library_until_it_has_text(factory):
    assert await repo.count_recordings(_USER) == 0

async def test_deleting_a_running_task_then_writing_content_is_a_noop(factory):
    await delete_task_with_recording(session, task); await session.commit()
    await Repo(session2).set_task_content(stale_task_obj, transcript_path="/x")  # no raise

async def test_post_api_tasks_creates_recording(client): ...  # through the HTTP endpoint
```

- [ ] Steps 2–4 as usual; run `tests/test_recordings*.py tests/test_tasks*.py tests/mcp -q`.
- [ ] **Step 5: Commit** `feat(recordings): create the recording together with its task (vts-lzad)`.

### Task 7: route pipeline and restart writers through the setters

**Files (each direct write replaced):**
- `vts/pipeline/steps/transcription.py:584` and `:712` → `repo.set_task_content(task, transcript_path=…)`
- `vts/pipeline/context.py:206` `persist_detected_language` → `repo.set_detected_language`
- `vts/pipeline/context.py:155` `persist_prompt_result` — unchanged call, now reaches the recording via `set_task_prompt_results`
- `vts/pipeline/steps/summarization.py` redacted writes (`:821`, `:839`, `:950`, `:995`) → after the step writes the file, `set_task_content(task, redacted_path=str(path))` once per step run (new `PipelineContext.persist_content(task_id, **fields)` helper wrapping a session + `set_task_content`)
- `vts/pipeline/steps/summarization.py:1274`, `:1470` → `set_task_content(task, summary_path=…)`
- `vts/services/summary_restart.py:57-58` → `downgrade_all_result_entries(task)` then `mirror_prompt_results(task)`; `set_task_content(task, summary_path=None, redacted_path=None)`
- `vts/services/summary_restart.py:95-100` → after `clear_all_finalize_results(task)`: `mirror_prompt_results(task)`; `set_task_content(task, summary_path=None)`
- `vts/api/routers/tasks.py:462` → `mirror_prompt_results(task)` after `downgrade_system_summary_entry(task)`; `:496` → `set_task_content(task, summary_path=None)`
- Test: `tests/test_recording_content.py`, existing `tests/test_merge_transcript_step.py`, `tests/test_finalize_loop.py`, `tests/test_pipeline_resume.py`

**Interfaces:**
- Consumes: Task 5 setters.
- Produces: `PipelineContext.persist_content(self, task_id: uuid.UUID, **fields) -> None`.

- [ ] **Step 1: Write the failing tests** — one per writer family, asserting the recording side after the writer ran:
  - merge_transcript run (existing test harness in `test_merge_transcript_step.py`) → `rec.transcript_path == str(outputs/"transcript.txt")`
  - `persist_detected_language(task_id, "russian", 0.9)` → `rec.language == "ru"`
  - finalize system summary → `rec.summary_path` set; a user prompt → `rec.prompt_results[0]["name"] == "Memo"`
  - `reset_task_for_summary_restart` → `rec.summary_path is None`, `rec.redacted_path is None`, prompt entries downgraded identically on task and recording
  - `restart_final_with_prompts` → `rec.prompt_results == []`
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** the replacements above (no behaviour change on the task side). **Step 4: Run** the listed files plus the full backend suite — PASS.
- [ ] **Step 5: Commit** `refactor(pipeline): write content through the recording setters (vts-lzad)`.

### Task 8: donor clone fills its recording

**Files:**
- Modify: `vts/pipeline/processor.py:694` (`_clone_from_donor`)
- Test: `tests/test_process_task_donor_clone.py` (extend)

**Interfaces:**
- Produces: after the copy, `set_task_content(task, transcript_path=_remap(…), summary_path=_remap(…), redacted_path=<task_dir/outputs/redacted_transcript.txt if it exists>)`; prompt results = donor entries whose `(source, id)` is in the clone's `options["prompts"]` and whose remapped path exists, via `set_task_prompt_results`; then the existing `set_task_status(completed)` (→ `complete`) and `reindex_task` so the clone is searchable. `match_decisions` are not copied (per-user voice registry).

- [ ] **Step 1: Failing test:** clone a donor with summary + one user prompt result + one prompt the clone did not request → recording has `state == complete`, `summary_path` under the clone dir, `prompt_results` == only the requested entry with remapped path, and appears in `list_recordings`.
- [ ] Steps 2–4. **Step 5: Commit** `fix(clone): a cloned task gets its library recording (vts-lzad)`.

### Task 9: backfill migration

**Files:**
- Create: `vts/services/recording_backfill.py`
- Create: `alembic/versions/0034_recording_content_backfill.py` (`down_revision = "0033_recording_content"`; downgrade refuses like 0031)
- Test: `tests/test_recording_backfill.py`

**Interfaces:**
- Produces: `def backfill_recording_content(conn, *, artifacts_available: bool) -> dict[str, int]` returning counts `{"created": n, "redacted": n, "prompt_results": n}`. For each task without a recording: insert one (fields as `upsert_recording_for_task`, `state=initial_state(...)`). For each recording with a task: `redacted_path` = `<artifact_dir>/outputs/redacted_transcript.txt` if the file exists; `prompt_results` = task `options["prompt_results"]` entries whose `path` exists. When `artifacts_available` is False (volume not mounted — detect as 0031 does), only the "created" part runs and the rest is skipped with a printed notice.

- [ ] **Step 1: Failing tests** with `tmp_path`: task without recording → created with correct state; archived task whose redacted file is gone → `redacted_path is None`, its missing prompt result dropped, present one kept; `artifacts_available=False` → created only, counts reflect it; running twice changes nothing the second time.
- [ ] Steps 2–4. **Step 5: Commit** `feat(recordings): backfill content fields from existing tasks (vts-lzad)`.

### Task 10: `listed_at`, library order, cursors and `task_ids` in API and MCP

**Files:**
- Modify: `vts/db/models.py` (`Recording.listed_at: Mapped[datetime | None]`, `DateTime(timezone=True)`, nullable; index `ix_recordings_user_listed` on `(user_id, listed_at, id)`)
- Create: `alembic/versions/0035_recording_listed_at.py` (`down_revision = "0034_recording_content_backfill"`): add column + index; `UPDATE recordings SET listed_at = created_at WHERE transcript_path IS NOT NULL AND listed_at IS NULL`; downgrade drops index and column
- Modify: `vts/db/repo.py` (`set_task_content`, `upsert_recording_for_task`, `_apply_recording_filters`, `list_recordings`, `count_recordings`)
- Modify: `vts/services/recording_backfill.py` (recordings it creates with a transcript get `listed_at = created_at`)
- Modify: `vts/api/schemas.py` (`RecordingOut.listed_at: datetime | None`), `vts/api/routers/recordings.py` (`list_recordings` params), `vts/mcp/schemas.py` + `vts/mcp/tools_registry/recordings.py` (`RecordingInfo.listed_at`, same params), `docs/API.md`
- Test: `tests/test_recording_listed_at.py`, extend `tests/test_recordings_filters.py`, `tests/mcp/test_recording_tools.py`

**Interfaces:**
- Produces: `listed_at` is set to `utcnow()` by `set_task_content` / `upsert_recording_for_task` exactly when the recording's `transcript_path` goes from NULL to non-NULL and `listed_at` is NULL; never cleared or moved afterwards.
- Produces: `_apply_recording_filters` uses `Recording.listed_at.isnot(None)` (replacing `transcript_path.isnot(None)`) and accepts `before: tuple[datetime, uuid.UUID] | None`, `after: tuple[datetime, uuid.UUID] | None` (strict keyset on `(listed_at, id)`); `task_ids` keeps its meaning (filter by `source_task_id`). `list_recordings(..., before=None, after=None, order="desc")` orders by `(listed_at, id)` in `order`; `count_recordings` ignores cursors.
- Produces: `GET /api/recordings` new optional params `before_ts`+`before_id`, `after_ts`+`after_id` (each pair together or 422), `order` (`asc|desc`, default `desc`), `task_id` (repeatable → `task_ids`). `limit`/`offset` keep working (external clients). Same params on MCP `list_recordings` (`task_id` as `task_ids: list[uuid.UUID] | None`).

- [ ] **Step 1: Failing tests** (real DB):
  - `test_listed_at_set_on_first_transcript_and_never_moves`: create task → `listed_at is None`; `set_task_content(transcript_path=…)` → set; second call with another path and a summary restart → unchanged.
  - `test_library_orders_by_listed_at_not_created_at`: task A created first, B second; B gets text first, then A → `list_recordings` ids == [A, B].
  - `test_cursor_pages_do_not_overlap_after_insert_on_top`: 3 listed recordings, page 1 (limit 2), a 4th becomes listed, page 2 via `before` cursor of page-1 tail → exactly the remaining 1, no duplicate; `after` cursor of page-1 head with `order="asc"` → exactly the new one.
  - `test_task_ids_filter_returns_that_tasks_recording` (HTTP `?task_id=`) and 422 on a half cursor pair.
  - migration helper test: a recording with transcript and NULL `listed_at` gets `created_at`; one without transcript stays NULL.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the new file, `tests/test_recordings*.py tests/test_recording*.py tests/mcp/` and the full backend suite — PASS.
- [ ] **Step 5: Commit** `feat(library): order the library by when a recording entered it, with cursor paging (vts-lzad)`.

### Task 11: library banner, in-place state updates, cursor paging in the web UI

**Files:**
- Modify: `vts/static/app.js` (`libraryPaging`, `loadLibrary`, `loadLibraryPage`, the `task_status` and `transcript_segment_text` SSE listeners, `applyLibraryMeta`/card render)
- Modify: `vts/static/index.html` (banner element `#new-recordings-banner` with `#new-recordings-count`, placed above the library list, before the `<script>` tag — app.js has no `defer`)
- Modify: `vts/static/i18n/en.js`, `ru.js`, `de.js`
- Create: `tests/ui/scenarios/library-new-recordings-banner.mjs`, `tests/ui/scenarios/library-state-updates-live.mjs`
- Modify (generated): `docs/ui-inventory.md` via `make ui-inventory`

**Interfaces:**
- Consumes: Task 10 API (`before_ts/before_id`, `after_ts/after_id`, `order`, `task_id`, `listed_at` in items).
- Produces: i18n key `library.new_recordings` = "New recordings" / "Новые записи" / "Neue Aufnahmen" (count shown as "(N)" in `#new-recordings-count`, same pattern as `#new-tasks-banner`).
- Behaviour:
  - `loadLibraryPage` pages by `before` cursor of the last loaded item (`listed_at`, `id`), not offset; keeps sending the library filters.
  - Library head cursor = `(listed_at, id)` of the top loaded item.
  - On `task_status` or `transcript_segment_text` for a task id with no library card, while the library has been loaded: fetch `/api/recordings?task_id=<id>&limit=1` with the current filters (debounced per task, at most one in flight per task); if an item comes back and is newer than the head → add to `libraryNewIds`, show the banner. The pattern mirrors `maybeFlagNewerTask` / `showNewTasksBanner`.
  - Banner click → fetch `after` the head with `order=asc` and current filters, prepend newest-on-top, drop loaded ids from `libraryNewIds`, hide the banner when a short page came back.
  - Filter change / user switch / refresh button clear `libraryNewIds` and hide the banner.
  - On `task_status` for a task whose library card is shown: fetch its recording by `task_id` and repaint that card's `.library-meta` state label in place (no list reload).

- [ ] **Step 1: Failing scenarios**, stubbed server like `library-recording-state.mjs`, with the SSE stub sending events:
  - banner: library open with 2 items; stub emits `task_status` for an unknown task whose `/api/recordings?task_id=` returns a newer listed recording → banner visible with "(1)"; click → that card is first, the 2 old cards still present below in order, banner hidden, no full reload (assert the old card DOM nodes are the same objects, e.g. via a marker attribute set before the event).
  - not newer / not listed: `task_id` lookup returns nothing → no banner.
  - live state: a shown "processing" card; stub emits `task_status` completed and `/api/recordings?task_id=` now returns `state: "complete"` → the label disappears from that card without a reload.
  - cursor paging: scrolling loads page 2 with `before_ts/before_id` of the last card (assert the request URL in the stub).
- [ ] **Step 2: Run** each scenario alone (fork wrapper, see Task 4 note) — FAIL.
- [ ] **Step 3: Implement**; `make ui-inventory`.
- [ ] **Step 4: Run** the new scenarios, the full UI suite and `make ui-inventory-check` — PASS. Revert the banner wiring once and confirm the banner scenario fails, then restore.
- [ ] **Step 5: Commit** `feat(library): announce new recordings and update states live, like the task list (vts-lzad)`.

### Release B checkpoint

- [ ] Full backend + UI suites green; `make ui-inventory-check`.
- [ ] Pre-tag checks; ask Victor for the tag.
- [ ] After deploy, on prod via the query wrapper: every task has a recording (`tasks LEFT JOIN recordings … WHERE recordings.id IS NULL` → 0); the migration's printed counts in the migrate container log; a fresh task shows a recording that stays out of the library until its first transcript segment, then appears as "processing".
- [ ] Update bd `vts-lzad` notes with what shipped; next: plan for stages 2–3.
