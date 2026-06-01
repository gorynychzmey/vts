# Inline rename of a task name in the VTS web UI

**Date:** 2026-06-01
**Status:** Approved (ready for implementation plan)

## Problem

A task's display name in the web UI comes from `Task.source_title`. For
uploads (OBS / file upload) it is whatever `display_name` was sent at
upload time, falling back to the `file://<filename>` source label when
unset. Users want to correct or set that name *after* upload, directly in
the UI — without re-uploading.

Originally we considered an OBS-side "ask for name before upload" dialog,
but OBS scripts have no reliable in-process modal text input during the
`RECORDING_STOPPED` event. Editing the name in VTS after upload is more
robust and works for every task, not just OBS ones.

## Scope

- Inline rename available on **all tasks** (uploads and URL-downloaded).
- A pencil icon next to the task name; clicking it turns the name into an
  input with ✓ (save) and ✕ (cancel) buttons.
- Save persists to `Task.source_title` via a new PATCH endpoint.
- Empty/whitespace name clears the title → UI falls back to the source
  label (existing `renderTaskTitle` behaviour).

**Out of scope:** editing any other task field; bulk rename; rename in the
MCP API (the field is already returned there as `title`).

## Backend

### Endpoint

`PATCH /api/tasks/{task_id}`

- Request body (Pydantic `TaskUpdate`): `{ "display_name": str | None }`.
- Auth: owner only. If the task does not exist or belongs to another user,
  return 404 (same opaque behaviour as the other task routes — never leak
  existence).
- Logic: `task.source_title = normalize_display_name(display_name)`,
  commit, return the updated `TaskOut` (same serializer as GET).
- Reuses the existing `normalize_display_name` helper from
  `vts/api/main.py` (whitespace-only → `None`; otherwise trimmed and
  capped at 500 chars).

### Repo

New method `Repo.update_task_source_title(task_id, user_id, title) -> Task | None`:

- Loads the task scoped to `user_id`; returns `None` if not found.
- Sets `source_title = title`, flushes, returns the task.
- Caller (endpoint) maps `None` → 404.

## Frontend

DOM is built by cloning `<template id="task-template">` in
`renderTasks()`. The name lives in `.task-title-row` as `.task-link`.

### HTML (`vts/static/index.html`, inside `.task-title-row`)

Add after `.task-expired`:

- `<button class="icon-btn ghost task-edit-name-btn">` — pencil icon
  (inline SVG, matching the other `.icon-btn` buttons).
- `<span class="task-name-edit hidden">` containing:
  - `<input class="task-name-input" type="text" maxlength="500">`
  - `<button class="icon-btn ghost task-name-ok-btn">` — check icon
  - `<button class="icon-btn ghost task-name-cancel-btn">` — cross icon

### JS (`vts/static/app.js`)

In `renderTasks()`: query the new nodes, store them on `taskEl._elements`,
and wire handlers.

New functions:

- `enterTitleEdit(taskEl)` — set `taskEl._editingTitle = true`; hide
  `linkEl` + pencil; show the edit span; prefill the input with
  `runtime.displayName || uploadName || sourceUrl`; focus + select.
- `commitTitleEdit(taskEl)` — disable the OK button; `PATCH
  /api/tasks/{id}` with `{ display_name }`; on success update
  `runtime.displayName` from the response and call `renderTaskTitle`; on
  error show an inline message and keep the editor open. Clears
  `_editingTitle` on success/cancel.
- `cancelTitleEdit(taskEl)` — clear `_editingTitle`, restore view mode, no
  request.

Keyboard: Enter → commit, Escape → cancel.

**Re-render safety:** background `renderTaskRuntime` fires on status
events. `renderTaskTitle` must early-return while `taskEl._editingTitle`
is true, so a status update never wipes the in-progress input.

The `app.js`-has-no-`defer` rule (new statically-referenced DOM must
precede the `<script>` tag) does not apply here: these nodes live inside a
`<template>` cloned at runtime and are reached via `querySelector` on the
clone, not `getElementById` at load.

## i18n + styles

- New keys in both locales (ru + en): `action.edit_name`,
  `action.save_name`, `action.cancel_edit` (used for `title` / `aria-label`).
- CSS in `vts/static/style.css`: `.task-name-input` and compact ok/cancel
  buttons, styled like existing `.icon-btn.ghost`.

## Error handling

- PATCH network error or 403/404 → inline error text near the editor;
  editor stays open so the user can retry or cancel.
- OK button disabled during the in-flight request to prevent double-submit.

## Testing

- **Backend (automated):** unit test for
  `Repo.update_task_source_title` semantics (updates field; returns None
  for wrong user / missing task) following the project's FakeRepo style;
  the `normalize_display_name` behaviour is already covered by
  `tests/test_upload_display_name.py`.
- **Frontend:** the project has no JS test harness, so the editor is
  verified manually via the running app (screenshot/`/run`) after build.

## Known limitation

For URL-downloaded tasks, `source_title` is set from video metadata during
download (`processor.py:_save_task_source_title`). A full task **restart**
re-downloads and overwrites a manually-set name. This is rare (restart =
re-download) and not worth guarding against; documented here so it is not a
surprise.
