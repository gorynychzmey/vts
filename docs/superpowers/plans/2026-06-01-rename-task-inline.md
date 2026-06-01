# Inline Task Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users rename any task's display name inline in the web UI via a pencil → input + ✓/✕ flow, persisted through a new `PATCH /api/tasks/{task_id}` endpoint.

**Architecture:** A new `PATCH /api/tasks/{task_id}` endpoint reuses `repo.get_task_for_user` (owner-scoped), sets `task.source_title` via the existing `normalize_display_name` helper, commits, and returns the standard `TaskOut`. The frontend adds a pencil button to the task title row that swaps the name for an input with save/cancel buttons; an `_editingTitle` flag protects the in-progress edit from background re-renders.

**Tech Stack:** FastAPI + Pydantic + SQLAlchemy (async) backend; vanilla JS + `<template>` cloning frontend; i18n via per-locale JS dicts (en/ru/de).

---

## File Structure

- `vts/api/schemas.py` — add `TaskUpdate` request model.
- `vts/api/main.py` — add `PATCH /api/tasks/{task_id}` endpoint (next to `get_task`).
- `vts/static/index.html` — add pencil button + edit span to `<template id="task-template">`.
- `vts/static/app.js` — wire up nodes in `renderTasks`; add enter/commit/cancel edit functions; guard `renderTaskTitle`.
- `vts/static/style.css` — styles for `.task-name-input` and ok/cancel buttons.
- `vts/static/i18n/{en,ru,de}.js` — three new action keys.
- `vts/__init__.py` — version bump.
- `tests/test_upload_display_name.py` — already covers `normalize_display_name`; add an endpoint-contract note. No new DB test (project has no async-DB test harness; verified manually).

---

## Task 1: Backend — `TaskUpdate` schema + PATCH endpoint

**Files:**
- Modify: `vts/api/schemas.py` (after `TaskCreateRequest`, ~line 22)
- Modify: `vts/api/main.py` (after `get_task`, ~line 1255)

- [ ] **Step 1: Add the `TaskUpdate` schema**

In `vts/api/schemas.py`, after the `TaskCreateRequest` class (line 22), add:

```python
class TaskUpdate(BaseModel):
    display_name: str | None = None
```

- [ ] **Step 2: Add the PATCH endpoint**

In `vts/api/main.py`, immediately after the `get_task` function (which ends at line 1255 with the `return serialize_task(...)`), add a new endpoint. Note `normalize_display_name` is already defined at module scope and `TaskUpdate` must be imported.

First, ensure `TaskUpdate` is imported. Find the existing schemas import line in `main.py` (search for `TaskOut` in an import) and add `TaskUpdate` to it.

Then add:

```python
    @app.patch("/api/tasks/{task_id}", response_model=TaskOut)
    async def update_task(
        task_id: uuid.UUID,
        payload: TaskUpdate,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        task.source_title = normalize_display_name(payload.display_name)
        await session.commit()
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)
```

- [ ] **Step 3: Verify the app imports and the route is registered**

Run:
```bash
.venv/bin/python -c "from vts.api.main import create_app; app = create_app(); print([r.path for r in app.routes if getattr(r, 'path', '') == '/api/tasks/{task_id}' and 'PATCH' in getattr(r, 'methods', set())])"
```
Expected: prints `['/api/tasks/{task_id}']` (the PATCH route is present).

- [ ] **Step 4: Verify the OpenAPI test still passes**

Run: `.venv/bin/python -m pytest tests/test_openapi_spec.py -q`
Expected: PASS (the route path already existed for GET; PATCH adds a method, not a new path — no assertion breaks).

- [ ] **Step 5: Commit**

```bash
git add vts/api/schemas.py vts/api/main.py
git commit -m "feat(api): PATCH /api/tasks/{id} to rename a task (display_name → source_title)"
```

---

## Task 2: Backend — endpoint normalization contract test

The endpoint delegates to `normalize_display_name`, already tested. Add an explicit test asserting the empty-name-clears-title contract so a future refactor can't silently change it.

**Files:**
- Modify: `tests/test_upload_display_name.py`

- [ ] **Step 1: Add the contract test**

Append to `tests/test_upload_display_name.py`:

```python
def test_blank_display_name_clears_title() -> None:
    # PATCH with a blank name must clear source_title (None), so the UI
    # falls back to the source label rather than showing an empty title.
    assert normalize_display_name("") is None
    assert normalize_display_name("   ") is None


def test_display_name_is_stored_trimmed() -> None:
    # A renamed task stores the trimmed value, not the raw input.
    assert normalize_display_name("  Standup  ") == "Standup"
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_upload_display_name.py -q`
Expected: PASS (all tests, including the two new ones).

- [ ] **Step 3: Commit**

```bash
git add tests/test_upload_display_name.py
git commit -m "test(api): rename endpoint clears/trims display_name contract"
```

---

## Task 3: Frontend — HTML template (pencil + edit controls)

**Files:**
- Modify: `vts/static/index.html` (`.task-title-row`, lines 225-228)

- [ ] **Step 1: Add the pencil button and edit span**

In `vts/static/index.html`, replace the `.task-title-row` block (currently lines 225-228):

```html
            <div class="task-title-row">
              <a class="task-link" target="_blank" rel="noopener noreferrer"></a>
              <span class="task-expired hidden" data-i18n="tasks.media_expired_badge" data-i18n-title="tasks.media_expired_tooltip"></span>
            </div>
```

with:

```html
            <div class="task-title-row">
              <a class="task-link" target="_blank" rel="noopener noreferrer"></a>
              <span class="task-expired hidden" data-i18n="tasks.media_expired_badge" data-i18n-title="tasks.media_expired_tooltip"></span>
              <button class="icon-btn ghost task-edit-name-btn" type="button" data-i18n-title="action.edit_name" data-i18n-aria-label="action.edit_name">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.58z"/></svg>
              </button>
              <span class="task-name-edit hidden">
                <input class="task-name-input" type="text" maxlength="500" />
                <button class="icon-btn ghost task-name-ok-btn" type="button" data-i18n-title="action.save_name" data-i18n-aria-label="action.save_name">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
                </button>
                <button class="icon-btn ghost task-name-cancel-btn" type="button" data-i18n-title="action.cancel_edit" data-i18n-aria-label="action.cancel_edit">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
                </button>
              </span>
            </div>
```

- [ ] **Step 2: Verify the HTML parses (page still loads)**

Run: `.venv/bin/python -c "from pathlib import Path; html = Path('vts/static/index.html').read_text(); assert html.count('task-name-edit') == 1 and html.count('task-edit-name-btn') == 1; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add vts/static/index.html
git commit -m "feat(ui): add pencil + edit controls to task title template"
```

---

## Task 4: Frontend — i18n keys (en/ru/de)

**Files:**
- Modify: `vts/static/i18n/en.js`, `vts/static/i18n/ru.js`, `vts/static/i18n/de.js` (near the `action.delete` entry, line 55)

- [ ] **Step 1: Add keys to en.js**

In `vts/static/i18n/en.js`, after the `"action.delete": "Delete",` line (line 55), add:

```javascript
"action.edit_name": "Rename",
"action.save_name": "Save name",
"action.cancel_edit": "Cancel",
```

- [ ] **Step 2: Add keys to ru.js**

In `vts/static/i18n/ru.js`, after the `"action.delete": "Удалить",` line, add:

```javascript
"action.edit_name": "Переименовать",
"action.save_name": "Сохранить имя",
"action.cancel_edit": "Отмена",
```

- [ ] **Step 3: Add keys to de.js**

In `vts/static/i18n/de.js`, after the `"action.delete": "Löschen",` line, add:

```javascript
"action.edit_name": "Umbenennen",
"action.save_name": "Name speichern",
"action.cancel_edit": "Abbrechen",
```

- [ ] **Step 4: Verify all three locales have the keys**

Run:
```bash
for f in en ru de; do grep -c "action.edit_name\|action.save_name\|action.cancel_edit" vts/static/i18n/$f.js; done
```
Expected: prints `3` three times (one per locale).

- [ ] **Step 5: Commit**

```bash
git add vts/static/i18n/en.js vts/static/i18n/ru.js vts/static/i18n/de.js
git commit -m "i18n: add rename/save/cancel action keys (en/ru/de)"
```

---

## Task 5: Frontend — wire up nodes + edit handlers in `renderTasks`

**Files:**
- Modify: `vts/static/app.js` (`_elements` literal at lines 1290-1321; handler wiring after line 1322)

- [ ] **Step 1: Add the new nodes to the `_elements` literal**

In `vts/static/app.js`, the `root._elements = { ... }` literal starts at line 1290. Add these five entries (e.g. right after the `sourceEl:` line, line 1293):

```javascript
      editNameBtn: root.querySelector(".task-edit-name-btn"),
      nameEditWrap: root.querySelector(".task-name-edit"),
      nameInput: root.querySelector(".task-name-input"),
      nameOkBtn: root.querySelector(".task-name-ok-btn"),
      nameCancelBtn: root.querySelector(".task-name-cancel-btn"),
```

- [ ] **Step 2: Wire the edit handlers**

After `root._runtime = createRuntime(task);` (line 1322) and BEFORE `renderTaskRuntime(root);` (line 1323), add:

```javascript
    const _els = root._elements;
    _els.editNameBtn.addEventListener("click", () => enterTitleEdit(root));
    _els.nameOkBtn.addEventListener("click", () => commitTitleEdit(root));
    _els.nameCancelBtn.addEventListener("click", () => cancelTitleEdit(root));
    _els.nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitTitleEdit(root); }
      else if (e.key === "Escape") { e.preventDefault(); cancelTitleEdit(root); }
    });
```

- [ ] **Step 4: Verify app.js parses**

Run: `node --check vts/static/app.js`
Expected: no output (exit 0). If `node` is unavailable, run `.venv/bin/python -c "import esprima" 2>/dev/null || echo "skip — no JS linter; will verify in browser"`.

- [ ] **Step 5: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(ui): wire task rename nodes and edit handlers in renderTasks"
```

---

## Task 6: Frontend — edit/commit/cancel functions + render guard

**Files:**
- Modify: `vts/static/app.js` (add functions near `renderTaskTitle`, ~line 1001; guard inside `renderTaskTitle`)

- [ ] **Step 1: Guard `renderTaskTitle` against clobbering an open editor**

In `vts/static/app.js`, at the very top of `renderTaskTitle(taskEl)` (line 1001, right after the function opening), add:

```javascript
  if (taskEl._editingTitle) {
    return;  // don't repaint the title while the user is editing it
  }
```

- [ ] **Step 2: Add the three edit functions**

Immediately before `function renderTaskTitle(taskEl) {` (line 1001), add:

```javascript
function enterTitleEdit(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  if (!runtime || !elements) return;
  const isUpload = typeof runtime.sourceUrl === "string" && runtime.sourceUrl.startsWith("file://");
  const uploadName = isUpload ? runtime.sourceUrl.slice("file://".length) : "";
  const prefill = runtime.displayName || uploadName || runtime.sourceUrl || "";
  taskEl._editingTitle = true;
  elements.linkEl.classList.add("hidden");
  elements.editNameBtn.classList.add("hidden");
  if (elements.expiredEl) elements.expiredEl.classList.add("hidden");
  elements.nameEditWrap.classList.remove("hidden");
  elements.nameInput.value = prefill;
  elements.nameInput.disabled = false;
  elements.nameOkBtn.disabled = false;
  elements.nameInput.focus();
  elements.nameInput.select();
}

function cancelTitleEdit(taskEl) {
  const elements = taskEl._elements;
  if (!elements) return;
  taskEl._editingTitle = false;
  elements.nameEditWrap.classList.add("hidden");
  elements.linkEl.classList.remove("hidden");
  elements.editNameBtn.classList.remove("hidden");
  renderTaskTitle(taskEl);
}

async function commitTitleEdit(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  if (!runtime || !elements) return;
  const value = elements.nameInput.value.trim();
  elements.nameOkBtn.disabled = true;
  elements.nameInput.disabled = true;
  try {
    const updated = await api(`/api/tasks/${encodeURIComponent(runtime.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: value }),
    });
    runtime.displayName = typeof updated.source_title === "string" ? updated.source_title.trim() : "";
    taskEl._editingTitle = false;
    elements.nameEditWrap.classList.add("hidden");
    elements.linkEl.classList.remove("hidden");
    elements.editNameBtn.classList.remove("hidden");
    renderTaskTitle(taskEl);
  } catch (err) {
    // Keep the editor open so the user can retry or cancel.
    elements.nameInput.disabled = false;
    elements.nameOkBtn.disabled = false;
    elements.nameInput.focus();
    console.error("rename failed", err);
  }
}
```

- [ ] **Step 3: Verify app.js parses**

Run: `node --check vts/static/app.js`
Expected: no output (exit 0). If `node` unavailable, note "verify in browser" and continue.

- [ ] **Step 4: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(ui): inline task rename enter/commit/cancel + render guard"
```

---

## Task 7: Frontend — styles

**Files:**
- Modify: `vts/static/style.css` (append near other `.task-*` / `.icon-btn` rules)

- [ ] **Step 1: Add styles**

Append to `vts/static/style.css`:

```css
.task-name-edit {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.task-name-input {
  font: inherit;
  padding: 2px 6px;
  border: 1px solid var(--border, #888);
  border-radius: 4px;
  min-width: 12rem;
  max-width: 100%;
  background: var(--input-bg, #fff);
  color: inherit;
}

.task-edit-name-btn svg,
.task-name-ok-btn svg,
.task-name-cancel-btn svg {
  width: 16px;
  height: 16px;
}
```

(If `style.css` defines theme variables like `--border` / `--input-bg` under different names, match the existing names — search the file first. The fallbacks keep it working regardless.)

- [ ] **Step 2: Verify the rule was added**

Run: `grep -c "task-name-input" vts/static/style.css`
Expected: prints `1` or more.

- [ ] **Step 3: Commit**

```bash
git add vts/static/style.css
git commit -m "style(ui): task rename input + ok/cancel button styles"
```

---

## Task 8: Version bump + full test suite + manual verification

**Files:**
- Modify: `vts/__init__.py`

- [ ] **Step 1: Bump the version**

In `vts/__init__.py`, increment the patch version (e.g. `1.0.92` → `1.0.93`).

- [ ] **Step 2: Run the full backend test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all tests green; new contract tests included).

- [ ] **Step 3: Manual UI verification**

Use the `/run` skill (or start the app) to:
1. Open the task list, confirm a pencil icon appears next to a task name.
2. Click it → name becomes an input with ✓/✕.
3. Type a new name, press Enter (or ✓) → name updates, persists after reload.
4. Click pencil again, clear the field, save → title falls back to the source label.
5. Click pencil, press Escape (or ✕) → reverts with no change.

- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py
git commit -m "chore: bump version for inline task rename"
```

- [ ] **Step 5: Push and close the issue**

```bash
git pull --rebase
git push
git status  # must show up to date with origin
```

---

## Notes for the implementer

- **No DB integration test:** this project tests the main API's DB layer indirectly (MCP tools use a `FakeRepo`; there is no aiosqlite async-DB harness). The endpoint logic is a thin wrapper over the already-tested `normalize_display_name` plus the established `get_task_for_user` pattern, so it is verified by reading + manual UI check rather than a new DB test. Do **not** add `aiosqlite` just for this.
- **`source_url` is untouched** by rename — it is the dedup key and `file://` download-skip marker. Only `source_title` changes.
- **Known limitation:** a full restart of a URL-downloaded task re-downloads and overwrites a manual name (see the design doc). Not guarded.
- **Beads:** create/claim an issue for this work before starting (`bd create ... --type=feature`), close it after push.
