# Task/Recording split — stages 2 and 3 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archiving keeps what the recording owns, every content reader reads the recording, and the task stops holding content fields.

**Architecture:** Release C (stage 2): fix archiving first, build one read layer on the recording, make task content endpoints/tools/delivery thin adapters over it, move library cards and `TaskOut` onto it. Release D (stage 3): writers stop dual-writing; a reversible migration drops the task's content columns and the `meta` snapshot.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / Alembic / Postgres / vanilla JS / Playwright UI scenarios.

**Spec:** `docs/superpowers/specs/2026-11-20-task-recording-split-design.md` — sections "Ownership split", "Decisions", "Stage 2", "Stage 3".

## Global Constraints

- Tests on Postgres only: `VTS_TEST_DATABASE_URL=postgresql+asyncpg://vts:vts@localhost:5432/vts_test_lzad .venv/bin/python -m pytest` from `/home/victor/dev/vts.worktrees/lzad` (own DB; the default is shared and fixtures `drop_all`). UI: `node tests/ui/run.mjs`; one scenario needs a fork wrapper (scenario-child.mjs uses `process.send`).
- JSON columns reassigned, never mutated in place.
- Version: Release C is 1.10.1, release D a patch on top (Victor 2026-11-22: minor only with new functionality — tags). Check `git tag -l "build-<v>"` and main before each bump.
- Narrow `git add`; no tree-wide stash/checkout/clean; never commit `tests/ui/node_modules`.
- Public repo: no private hosts/paths/keys.
- English in code/comments/commits; reference `vts-lzad`.
- Old public addresses keep working: `/api/tasks/{id}/transcript|summary|redacted|subtitles|transcript-entries|results/...`, `/player/{task_id}?t=`, MCP `get_transcript(task_id)`, `get_prompt_result(task_id)`.
- Speaker naming, summary restart and deliveries stay task-addressed.
- Another agent's code (MCP Apps, `vts/mcp/tools_registry/apps.py`) reads `Recording.meta["prompt_results"]`; Task 2 switches it to the column — keep its tests green: `tests/mcp/test_player_view.py tests/mcp/test_regenerate_summary.py tests/mcp/test_speakers_view.py tests/test_recording_media_signed.py tests/test_player_blocks_recording.py tests/test_restart_final_prompts.py`.

## Review Focus

1. **Archived task, recording still readable**: after archiving, the library card's transcript, processed transcript, summary, Memo tab, subtitles and player sentences all still load. Test in Task 1 (backend) and Task 5 (UI).
2. **A task with no recording** (pre-backfill legacy or a race): task adapters answer 404 "not ready", never 500. Test in Task 3.
3. **A recording whose file is gone**: endpoints answer 404 / tab absent, never 500. Test in Task 3.
4. **Summary regeneration in progress**: `summary_path` NULL on the recording → summary tab shows "not ready", Memo tabs still show their files. Test in Task 3.
5. **Detached library card** (no `source_task_id`): content tabs load via the recording; speaker/restart controls are not offered. Test in Task 5.

---

## Stage 2 — release C

### Task 1: archiving keeps what the recording owns

**Files:**
- Modify: `vts/api/_helpers/artifact_store.py:25` (`_archive_task_artifacts`)
- Test: `tests/test_api_task_progress.py` (update `test_archive_task_artifacts_keeps_transcript_and_summary` ~:217), new `tests/test_archive_keeps_recording.py`

**Interfaces:**
- Produces: `RECORDING_OWNED_RELPATHS: tuple[str, ...]` in `artifact_store.py` = `("outputs/transcript.txt", "outputs/transcript.json", "asr/segments_raw.json", "outputs/redacted_transcript.txt", "outputs/summary.md", "outputs/summary.json", "summary/final.md", "summary/final.json")` plus the directory prefix `"summary/results/"` (every file under it kept). `_archive_task_artifacts(task)` keeps those (when inside the dir), plus `task.transcript_path` / `task.summary_path` as today, and `logs/task.log` (rewritten to the archived marker); deletes everything else (media, other intermediates, speaker-naming inputs, `language_detection.json`).

- [ ] **Step 1: Failing test** `test_archive_keeps_every_recording_owned_file`: build an artifact dir with every file above plus `media/audio_16k.wav`, `outputs/diarization.json`, `outputs/speaker_matches.json`, `summary/windows.json`, `summary/results/user__p1.md`; archive; assert exactly the kept set exists and the rest is gone. Plus `test_recording_readers_survive_archive`: with a real recording, after archive `read_recording_transcript(rec, "redacted")`, `recording_transcript_entries(rec)` and `player_blocks_from_artifacts(...)` return non-empty and the prompt result file is readable.
- [ ] **Step 2: RED.** **Step 3: Implement.** **Step 4:** run new + `tests/test_api_task_progress.py tests/test_recording*.py` — PASS.
- [ ] **Step 5: Commit** (with the release version bump) `fix(archive): keep what the recording owns (vts-lzad)`.

### Task 2: one read layer on the recording

**Files:**
- Modify: `vts/services/recording_artifacts.py` (`_artifact_path`, new functions)
- Modify: `vts/api/routers/recordings.py:47` (`_serialize`), `vts/mcp/tools_registry/recordings.py:100` (`recording_prompt_result`), `vts/mcp/tools_registry/apps.py:112` (`prompt_results_index`)
- Test: `tests/test_recording_artifacts.py`, `tests/test_recording_serialization.py`, `tests/mcp/test_recording_tools.py`, `tests/mcp/test_player_view.py`

**Interfaces:**
- Produces in `recording_artifacts.py`:
  - `_artifact_path(recording, kind)` — `redacted` uses `recording.redacted_path`, falling back to `outputs/redacted_transcript.txt` under `artifact_dir` only when `redacted_path` is NULL (legacy).
  - `recording_result_entries(recording) -> list[dict]` — the `prompt_results` column (dict entries only).
  - `recording_result_path(recording, source: str, ref: str) -> str | None` — the entry with that `(source, id)` and a path; for `("system", "summary")` falls back to `recording.summary_path`. Mirrors `vts/services/prompt_results.resolve_result_path`.
  - `read_recording_result(recording, source, ref) -> str` — reads the file under `artifact_dir` (reuse the path-escape guard now in `tools_registry/recordings.py::_read_result_file`, moved here); raises `RecordingArtifactMissing` when no entry/file.
- `_serialize` → `prompt_results` from the column, `has_redacted` from the redacted path existing; `recording_prompt_result` and `prompt_results_index` → the column via these functions.

- [ ] **Step 1: Failing tests**: column wins over `meta` (meta lists a stale entry, column lists the real one → only the column's appears in `RecordingOut.prompt_results`, MCP `get_recording_prompt_result`, `prompt_results_index`); `redacted_path` pointing to a non-default file is read; system summary falls back to `summary_path`; missing file → `RecordingArtifactMissing`.
- [ ] Steps 2–4; run the listed tests and the MCP Apps tests from Global Constraints.
- [ ] **Step 5: Commit** `refactor(recordings): read results from the recording's own fields (vts-lzad)`.

### Task 3: recording content endpoints; task endpoints become adapters; player by either id

**Files:**
- Create: `vts/api/_helpers/recording_content.py` (shared responders)
- Modify: `vts/api/routers/recordings.py` (new routes), `vts/api/routers/artifacts.py` (`get_transcript` :312, `get_transcript_entries` :343, `get_subtitles` :375, `get_summary` :419, `get_redacted_transcript` :463, `media_player` :548), `vts/api/routers/tasks.py:138` (`get_prompt_result`), `vts/api/_helpers/artifact_store.py:261` (`_load_player_blocks` delegates to `load_recording_player_blocks`), `vts/mcp/tools_registry/search.py:70-75` (player link by recording id), `docs/API.md`
- Test: `tests/test_recording_content_api.py`, existing `tests/test_text_artifact_serve.py tests/test_subtitles.py tests/test_player_page.py tests/test_prompts_api.py tests/test_transcript_updated_event.py tests/test_openapi_spec.py`

**Interfaces:**
- Produces in `recording_content.py` (all take a loaded `Recording`; 404 `HTTPException` with today's messages when not ready / file missing):
  - `serve_recording_text(recording, variant: Literal["raw","redacted","summary"], *, request, offset, limit) -> Response` — same media types and slicing as today's task endpoints (reuse `_serve_text` from artifacts.py — move it here if needed and import back).
  - `recording_entries(recording, session) -> dict` → `{"blocks": await load_recording_player_blocks(recording, session)}`.
  - `recording_subtitles(recording, session) -> Response` → `render_webvtt(...)` text/vtt.
  - `recording_result(recording, source, ref) -> PlainTextResponse`.
  - `recording_for_task_or_404(repo, user_id, task_id) -> Recording` (task ownership check, then `recording_for_task`; no recording → 404 "Transcript is not ready").
- New routes (session auth, owner check via `get_recording_for_user`): `GET /api/recordings/{id}/text/{variant}` (`variant` raw|redacted|summary; `offset`, `limit`, Range), `GET /api/recordings/{id}/entries` (include_in_schema=False), `GET /api/recordings/{id}/subtitles`, `GET /api/recordings/{id}/results/{source}/{ref}`. The existing JSON `GET /api/recordings/{id}/transcript` is unchanged.
- Task routes keep paths, params and responses; bodies become `recording_for_task_or_404` + the responder.
- `GET /player/{id}`: resolve `id` as a task of the user, else as a recording of the user (then its `source_task_id` for media; no task → transcript-only page without media). Blocks from `load_recording_player_blocks`. Live script only when a task exists.
- `search_transcripts` `player_url` → `/player/{recording_id}`.

- [ ] **Step 1: Failing tests**: each new route returns the same body/media type as its task twin for the same data; task routes still pass their existing tests; task with no recording → 404; recording file missing → 404 (not 500); summary NULL during regeneration → 404 on summary, prompt result still 200; `/player/{recording_id}` renders with the recording's blocks; `/player/{task_id}?t=` unchanged.
- [ ] Steps 2–4; run the listed test files plus the full backend suite.
- [ ] **Step 5: Commit** `feat(api): serve content from the recording; task routes adapt (vts-lzad)`.

### Task 4: `TaskOut` content fields from the recording

**Files:**
- Modify: `vts/api/_helpers/serialization.py` (`serialize_task` :268, `serialize_task_compact` :331, `_task_stats_for_serialization` :186), callers in `vts/api/routers/tasks.py` (list/get/update/create), `vts/db/repo.py` (`recordings_for_tasks(task_ids) -> dict[uuid.UUID, Recording]`, one query)
- Test: `tests/test_api_task_progress.py`, `tests/test_awaiting_step_cleared.py`, new cases in `tests/test_recording_content_api.py`

**Interfaces:**
- `serialize_task(task, ..., recording: Recording | None = None)` and `serialize_task_compact(..., recording=None)`: `transcript_path`, `summary_path`, `redacted_path` (existence), `stats.transcript_chars/summary_chars/redacted_chars` and `options["prompt_results"]` in the output come from the recording (output-only: build a new options dict, never mutate `task.options`); `speaker_count` stays task-side (speaker_matches.json). With `recording=None` fall back to today's task fields (stage 2 still dual-writes).
- List endpoints load recordings for the page with `recordings_for_tasks` (no N+1).

- [ ] **Step 1: Failing test**: task and recording disagree on purpose (recording has redacted_path + a prompt result the task lacks) → `TaskOut` shows the recording's; list endpoint issues one recordings query for a page (count statements or assert via a repo spy).
- [ ] Steps 2–4. **Step 5: Commit** `refactor(api): task cards show the recording's content fields (vts-lzad)`.

### Task 5: library cards read through the recording

**Files:**
- Modify: `vts/static/app.js` (`recordingAsTask` :8387, `loadTabContent` :642, `loadPromptResult` :871, `loadShareContent` :972, player links :1620/:2303/:8986, card expand :2644)
- Create: `tests/ui/scenarios/library-card-reads-recording.mjs`
- Modify (generated): `docs/ui-inventory.md`

**Interfaces:**
- Consumes: Task 3 routes.
- A card with `cardKind === "recording"` builds content URLs from the recording id: transcript `/api/recordings/{rid}/text/raw`, subtitles `/api/recordings/{rid}/subtitles`, redacted `/text/redacted`, summary `/text/summary`, prompt results `/api/recordings/{rid}/results/{source}/{id}`, player `/player/{rid}`. Task cards unchanged. Speaker panel and restart on a library card use `source_task_id` when present and are not offered when it is absent.

- [ ] **Step 1: Failing scenario** (stub server): a library card whose stub serves content ONLY under `/api/recordings/{rid}/...` (task routes 404) shows transcript, processed, summary and a Memo tab; a detached card shows content and no speaker/restart controls; a task card still uses `/api/tasks/{id}/...`.
- [ ] Steps 2–4 (scenario stable 3×, full UI suite, `make ui-inventory-check`).
- [ ] **Step 5: Commit** `feat(library): cards read their content from the recording (vts-lzad)`.

### Task 6: MCP task tools and delivery read through the recording

**Files:**
- Modify: `vts/mcp/tools.py` (`get_prompt_result` :480, `get_transcript` :1025, `_wait_condition_met` :1059), `vts/mcp/tools_registry/recordings.py:357` (`as_subtitles` cue labels from `load_recording_player_blocks` names), `vts/delivery/resolve.py` (`_task_meta` :19, `_resolve_prompt_variant` :38, `resolve_variant` :81), `vts/delivery/consumer.py:41`
- Test: `tests/mcp/test_tools_transcript.py`, `tests/mcp/test_tools_prompts.py`, `tests/mcp/test_tools_wait.py`, `tests/mcp/test_recording_tools.py`, `tests/delivery/test_resolve.py`, `tests/delivery/test_prompt_variant.py`, `tests/delivery/test_consumer.py`

**Interfaces:**
- MCP `get_transcript(task_id, variant)`, `get_prompt_result(task_id, ref)`: load task (ownership), then its recording, then the Task 2 read layer; same result models.
- `_wait_condition_met(task, until, recording)`: transcript/summary readiness from the recording (`wait_for_task` loads it).
- `as_subtitles`: cue speaker = the resolved name from the recording's player blocks, falling back to the label (today it prints raw `SPEAKER_NN`).
- Delivery: `resolve_variant(task, variant, recording)`; the consumer loads the recording by `source_task_id`; meta `language` from `recording.language`, `duration_s` from `recording.duration_sec` (today always None).

- [ ] **Step 1: Failing tests** for each: task/recording disagree on purpose → the recording's content wins; delivery payload carries `duration_s == recording.duration_sec`; `as_subtitles` shows the bound person's name.
- [ ] Steps 2–4 (+ full backend suite). **Step 5: Commit** `refactor(mcp,delivery): read content through the recording (vts-lzad)`.

### Release C checkpoint
- [ ] Whole-branch review (most capable model) of release C; one fix wave; full backend + UI suites; ask Victor for merge + `build-1.10.1`; after deploy verify an archived task's library card on prod still shows its texts (read-only check of files + `GET` through the app if possible).

---

## Stage 3 — release D

### Task 7: writers write the recording only

**Files:**
- Modify: `vts/db/repo.py` (`set_task_content`, `set_task_prompt_results`, `mirror_prompt_results`, `upsert_recording_for_task`), `vts/services/prompt_results.py` (helpers operate on the recording's entries), `vts/services/summary_restart.py`, `vts/api/routers/tasks.py` (restart paths), `vts/pipeline/context.py` (`persist_prompt_result`), `vts/pipeline/steps/transcription.py:711`, `vts/pipeline/steps/summarization.py:~1276`, `vts/pipeline/processor.py` (`_clone_from_donor` reads the donor's recording), `vts/services/recording_state.py` (`initial_state` callers pass recording fields), `vts/api/_helpers/serialization.py` (drop the task fallbacks from Task 4)
- Test: `tests/test_recording_content.py`, `tests/test_finalize_loop.py`, `tests/test_restart_final_prompts.py`, `tests/test_process_task_donor_clone.py`, `tests/test_prompts_repo.py`

**Interfaces:**
- `set_task_content(task, ...)` writes only the recording (rename is out of scope — keep the name); `set_task_prompt_results(task, entries)` writes only `recording.prompt_results`; `mirror_prompt_results` is removed (callers pass entries to `set_task_prompt_results`); prompt_results helpers become `downgrade_system_summary_entry(entries) -> list`, `downgrade_all_result_entries(entries) -> list`, `clear_all_finalize_results(...)` returning new entries (pure), callers write them via the setter.
- `upsert_recording_for_task` no longer copies `transcript_path`, `summary_path`, `meta.prompt_results` from the task; still sets title/source_url/duration/language.
- `options["detected_language"]` stays (pipeline runtime input).
- A grep-gate test `tests/test_no_task_content_fields.py`: no `\.transcript_path` / `\.summary_path` on a task object and no `options\["prompt_results"\]` in `vts/` outside `alembic/` and the backfill module (allow-list by file).

- [ ] Failing tests (task fields stay NULL after a full pipeline-shaped sequence; grep gate) → implement → full backend suite → **Commit** (with a patch bump) `refactor(recordings): the recording is the only writer target (vts-lzad)`.

### Task 8: reversible migration dropping the task's content columns

**Files:**
- Create: `alembic/versions/0036_drop_task_content_columns.py`, function in `vts/services/recording_backfill.py` for the downgrade refill
- Modify: `vts/db/models.py` (remove `Task.transcript_path`, `Task.summary_path`), schemas that referenced them only as ORM attributes
- Test: `tests/test_drop_task_content_migration.py`

**Interfaces:**
- Upgrade: drop `tasks.transcript_path`, `tasks.summary_path`; strip `prompt_results` from every `tasks.options`; strip `prompt_results` from every `recordings.meta`.
- Downgrade: re-add both columns; refill from each task's recording (`transcript_path`, `summary_path`), and `options.prompt_results` / `meta.prompt_results` from `recording.prompt_results` — so the 1.11.x image runs again.

- [ ] Failing test (upgrade then downgrade on a seeded DB → task fields equal the recording's) → implement → `alembic upgrade head` / `downgrade -1` / `upgrade head` on a scratch DB → full suites → **Commit** `feat(db): drop the task's content columns, reversibly (vts-lzad)`.

### Release D checkpoint
- [ ] Whole-branch review; fix wave; suites; ask Victor for merge + the next patch tag; after deploy verify migration 0036 and that new tasks complete with recording fields set and task columns gone.
