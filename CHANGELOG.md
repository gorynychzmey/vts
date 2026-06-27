# Changelog

All notable changes to this project are documented in this file.

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
