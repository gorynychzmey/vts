# Changelog

All notable changes to this project are documented in this file.

## 1.1.8 — Task option presets (vts-hp7)

Save a named bundle of task-creation options (language, audio-only,
transcript, prompt selection) as a preset and apply it when creating a task.

- A system read-only "Default" preset reproduces the form's out-of-the-box
  options; users create/edit/delete their own presets.
- The create form has a preset dropdown that fills the options on selection,
  with a single save button (Save as preset / Save changes) and a re-save
  hint when a preset references deleted prompts.
- A manager dialog supports create, edit, delete, duplicate (including
  duplicating system presets into editable user copies), and "make default".
- Each user has one active default preset (`users.default_preset`), which may
  be a system or a user preset; deleting the active default falls back to the
  system "Default".
- HTTP: `GET/POST/PATCH/DELETE /api/presets`, `GET/PUT /api/me/default_preset`.
  MCP: preset CRUD tools plus a `preset` parameter on `submit_video` that the
  server expands (preset options as the base; explicit fields override;
  deleted-prompt refs filtered).

(1.1.6 / 1.1.7 were UI fixes for the restart dialog and prompt selector,
plus MCP/HTTP cleanups — see git history.)

## 1.1.5 — Restart final stage with a different prompt set (vts-2or)

The task-card "Restart final summary only" action now opens a dialog with a
prompt multiselect, prefilled with the task's current set. Restarting reuses
the already-processed transcript (windows + pack) and regenerates the whole
finalize stage for the chosen set. Prompts removed from the set have their
results discarded; the task's set and its results stay consistent.

- HTTP: `POST /api/tasks/restart_summary` accepts an optional `prompts` list
  (only with `mode="final_only"`, non-empty); it swaps the task's prompt set,
  clears old finalize results, rebuilds the finalize step tail, and re-queues.
- The final-restart gate no longer requires the built-in summary to be in the
  set — any task with a completed processed transcript can restart its final
  stage.

## 1.1.0 — Custom prompts (VOS-63)

Users can now create custom prompts and choose, per task, which prompts to
apply to the transcript. Each selected prompt runs independently as its own
final pass over the prepared transcript and produces a separately stored
result. The built-in summary is now one selectable prompt among the set.

### ⚠️ Breaking changes

**Task creation no longer accepts a boolean `summary`.** Both the HTTP API
and the MCP tools now take a `prompts` list of `{source, id}` references
instead.

- **HTTP `POST /api/tasks`** and **`POST /api/tasks/upload`**: the `summary`
  (and alias `do_summary`) field is removed. Send
  `prompts: [{"source": "system", "id": "summary"}, ...]` instead. An empty
  list means "transcript only". For uploads, `prompts` is a JSON-encoded
  string form field.
- **MCP `submit_video`**: the `summary: bool` parameter is replaced by
  `prompts: list[{source, id}]` (defaults to the built-in summary when
  omitted).
- **MCP `get_summary` is removed.** Use `get_prompt_result(task_id, ref)`
  instead; pass `ref = "system:summary"` to fetch the built-in summary.

Old tasks created before this release continue to work: a missing `prompts`
key is interpreted from the legacy `summary` flag (`summary=false` → no
prompts, otherwise → the built-in summary).

### Added

- `prompts` table for per-user custom prompts; Alembic migration `0010_prompts`.
- HTTP prompt management: `GET/POST/PATCH/DELETE /api/prompts`,
  `GET /api/prompts/{id}` (detail), `GET /api/prompts/system/{key}/text`.
- HTTP `GET /api/tasks/{id}/results/{source}/{ref}` to read any prompt's result.
- MCP tools: `list_prompts`, `create_prompt`, `update_prompt`,
  `delete_prompt`, `get_prompt_result`.
- Web UI: a prompt multiselect on the task form, a prompt manager panel
  (create / edit / delete / duplicate, including duplicating built-in
  prompts), and a results dropdown to view each prompt's output.
- Pipeline: dynamic finalize stage — one step per selected prompt
  (`finalize:{source}:{id}`; the built-in summary keeps the
  `summarize_final` step name), each result saved separately.

### Notes

- Progress weights (`STEP_WEIGHT_SECONDS`) were not recalibrated in this
  release; finalize steps reuse the summary final-call estimate. Follow-up
  tracked in bd `vts-b6t`.
