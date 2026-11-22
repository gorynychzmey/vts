# Tags — design

Status: draft for review · bd: `vts-qwcj` · Linear: VOS-86 + VOS-146 (done together)
Builds on the task/recording split (`2026-11-20-task-recording-split-design.md`, shipped as 1.10.2).

## Intent

A tag puts a recording into an area or topic (example: "Business club" online
meetings) so the user can filter by it. Everything said about tasks applies to
library recordings too: a task shows its recording's tags, and there is one
set of tags per task + recording (no detaching, unlike the title).

## Requirements (Victor)

- Set tags when creating a task, and later on a running or finished task or a
  library recording — the same way renaming works.
- Input: type with suggestions from existing tags, or pick from a list. A tag
  that does not exist yet is created on save. Existing and new tags are shown
  in different colours while typing. A tag can be removed.
- Presets can carry tags: choosing a preset adds its tags to the new-task
  form, where they can be removed before submitting.
- Filter / search by tags in the task list and the library; with several
  tags, choose "all" or "any". The same from MCP.
- A tag management dialog: every tag with the number of recordings using it;
  an unused tag shows no number and a different colour; renaming; deleting a
  tag unlinks it everywhere.
- MCP can set tags too (`submit_video`, an add/remove tool) and list tags
  with counts.

## Decisions

### Data

- `tags` table: `id`, `user_id` (FK users, CASCADE), `name` (display form,
  the first spelling), `name_key` (normalised: trimmed, inner whitespace
  collapsed, case-folded), `created_at`. Unique `(user_id, name_key)` — tags
  are per user and case-insensitive (Victor: "Бизнес-клуб" = "бизнес-клуб").
- `recording_tags` link table: `(recording_id FK CASCADE, tag_id FK CASCADE)`
  primary key on both. Deleting a recording drops its links; deleting a tag
  unlinks it everywhere. Empty tags exist (the dialog needs them).
- Name rules: trimmed, inner whitespace collapsed, 1–64 characters after that;
  anything else is a 422.
- The unused `recordings.tags` JSON column is dropped.
- Presets store tag **ids** in `options["tags"]` (a rename follows; a deleted
  tag is skipped when the preset is read and removed from it on its next
  save).

### Behaviour

- **Setting tags is "replace the set by names"**: the client sends the full
  list of names; the server resolves each by `name_key`, creates the missing
  ones, and replaces the recording's links. One call covers add, remove and
  create.
- **Rename** to a name whose key already belongs to another tag of the user is
  a 409 ("already exists") — no silent merge. Renaming to a different
  spelling of the same key (case change) is allowed.
- **Count** in the dialog = recordings linked to the tag (a task and its
  recording are one material).
- **Filters**: `tags` (one or more names) + `tag_match` = `all` (default) |
  `any`. Unknown names: under `all` the result is empty; under `any` they are
  ignored. Applied in the shared filter helpers so list and count agree
  (`_apply_task_filters`, `_apply_recording_filters`).

### API

- `GET /api/tags` → `[{id, name, count}]` sorted by name; `POST /api/tags
  {name}` → create (409 if the key exists); `PATCH /api/tags/{id} {name}` →
  rename; `DELETE /api/tags/{id}` → delete + unlink.
- `PUT /api/recordings/{id}/tags {names: [...]}` → the recording's tags after
  the change; `PUT /api/tasks/{id}/tags` is the adapter (task → its
  recording).
- `RecordingOut.tags` and `TaskOut.tags` = `[{id, name}]` from the recording
  (one query per page for lists, like `recordings_for_tasks`).
- `POST /api/tasks`, the upload endpoints and finalize accept `tags: [names]`
  and set them on the recording created with the task.
- `GET /api/tasks`, `/api/tasks/count`, `GET /api/recordings` accept `tag`
  (repeatable) + `tag_match`.

### MCP

- `submit_video(..., tags=[names])`; `list_tasks` / `list_recordings`
  `tags=[names]`, `tag_match`; `list_tags()` → names with counts;
  `tag_recording(recording_id, add=[names], remove=[names])` (write-annotated). Task and
  recording summaries carry their tag names.

### Web UI

- **New-task form**: a tag field under the options: chips for chosen tags; a
  text input with a suggestion list of existing tags (typed text filters it);
  Enter or comma adds; existing tags render in the normal chip colour, new
  ones (not yet created) in the "new" colour; × removes. Choosing a preset
  adds its tags (no duplicates).
- **Preset dialog**: the same tag field.
- **Cards** (task and library): a tags row under the title. "Edit tags" in the
  card menu (next to Rename) turns it into the same field; save on Enter /
  blur, Esc cancels.
- **Filters**: the task filter panel and the library filter get a tag picker
  (chips) and an all/any toggle (shown when 2+ tags are chosen).
- **"Manage tags"** in the header menu (next to presets/prompts): a dialog
  listing tags with counts; unused tags without a number, in a muted colour;
  inline rename; delete with a confirmation that names the count it will
  unlink; a field to create a tag.
- Live updates: a tag change on a card patches that card (and the other list's
  card for the same task/recording if loaded); the manage dialog reloads on
  open.


### Builder-check answers (my calls unless marked)

- **System presets carry no tags.** They are code-defined and shared by all
  users (`vts/services/preset_registry.py`), while tags are per user. The tag
  field is shown only for user presets.
- **Counts include every linked recording**, also ones still processing and not
  yet listed in the library: the task list shows those tasks, and the tag is
  in use. The delete confirmation uses the same number.
- **Task-list filter** joins `recordings` on `source_task_id` (every task has
  its recording since 1.10.0; deleting a task deletes the recording and its
  links).
- **Normalisation is done in Python only** (`str.casefold()` after trimming and
  collapsing whitespace) and stored in `name_key`; the unique index is on
  `name_key`, never on a database-side `lower()`. Filters normalise the given
  names the same way.
- **Concurrency**: the web UI replaces the full set (last write wins — a user
  edits one card at a time). MCP gets an **add/remove** tool,
  `tag_recording(recording_id, add=[], remove=[])`, so an LLM that means to
  add one tag cannot wipe the others; `submit_video(tags=...)` sets the
  initial set.
- **Uploads**: tags are accepted wherever the task is created
  (`POST /api/tasks`, single upload, multi-file init); finalize does not take
  tags. A tag created for an upload that is later abandoned stays as an empty
  tag — empty tags are legal.
- **Delivery carries tags** (Victor, 2026-11-22): `TaskMeta.tags: tuple[str, ...]`
  — the recording's tag names at delivery time, sorted; empty when none. How
  an adapter uses them (frontmatter, properties, labels) is the adapter's
  business. Adding an optional field is additive → `CONTRACT_VERSION` minor
  bump (1.3 → 1.4), per the contract rule in `vts/delivery/contract.py`.

## Non-goals

- Tag hierarchies, colours chosen by the user, sharing tags between users.
- Automatic tagging.
- Filtering corpus search (`/api/search`) by tags — a later step on top of
  the same table.
- The speaker filter (`vts-tjn4`), though its UI should reuse the same picker
  pattern.

## Approaches considered

Chosen in the brainstorm (2026-11-20): tags as entities in their own table with
one link table to the recording (B), versus links to both task and recording
synced in code (A, rejected: two copies drift) and JSON strings on task and
recording (C, rejected: no empty tags, rename/delete rewrite every row). The
task/recording split made B simpler: links point at the recording only.

## Constraints

- Postgres in tests; JSON reassigned, not mutated; public repo rules; English
  in code; version: minor bump (new functionality — Victor: 1.11.0).
- Existing public routes keep working; new fields are additive.

## Left to the builder

- Exact markup/CSS of chips and the picker (reuse existing option-pill /
  menu styles; check mobile width).
- Migration split, index choices beyond the unique key.
- How the suggestion list is positioned and keyboard-navigated (arrow keys +
  Enter expected).
