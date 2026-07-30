# VOS-84 — Infinite scroll for the task list (+ SSE correctness)

**Linear:** [VOS-84](https://linear.app/vostrikov/issue/VOS-84/sdelat-paginaciyu-vyvoda-ili-skolzyashij-skroll)
**Date:** 2026-07-30
**Status:** Design approved, ready for implementation plan

## Scope

VOS-84 asks for three things: (1) infinite scroll / pagination, (2) a filter by
name/date/type, (3) matching MCP list methods. **This iteration delivers only
(1) plus a specific SSE-correctness fix.** The filter and MCP methods are
deferred to separate follow-up issues (VOS-84b / VOS-84c) — they are logically
independent of the scroll mechanics and would enlarge the diff and delay
release.

**In scope:**
- Infinite scroll on the web task list with a configurable page size.
- Correct handling of SSE status updates for tasks not yet loaded into the list.
- A "New tasks ↑" banner that appears when a task newer than the topmost loaded
  task arrives, and pulls those tasks in on click.
- Smooth prepend of the user's own newly created tasks (no full list rebuild).

**Out of scope (follow-up issues):**
- Filter by name / date / type (file/url).
- MCP list methods for the above.

## Background — current behaviour

- `GET /api/tasks` (`vts/api/main.py:2306`) already supports `limit`/`offset`/`compact`.
  These are used by MCP and ChatGPT Custom Actions and must not change.
  Repo method `list_tasks_for_user` (`vts/db/repo.py:91`) orders `created_at DESC`.
- The web client (`vts/static/app.js`) currently loads **all** tasks in one
  `loadTasks()` call (no limit) and `renderTasks()` does `taskList.innerHTML = ""`
  (full rebuild).
- **SSE**: one stream `GET /api/events` (`vts/api/main.py:2837`) filtered by
  `user_id`. Every event for every task of the user flows down it.
- **No JS error today** for updates to un-loaded tasks: `patchTaskStatus`
  (`app.js:2895`), `patchTaskProgress`, and `patchTaskStep` all begin with
  `if (!taskEl || !taskEl._runtime) return;`. The real gap is UX: a task that
  becomes `running`/`completed` while below the loaded window never appears
  until a full page reload.

## Design decisions (agreed)

1. **Scope:** scroll + SSE only; filter and MCP deferred.
2. **Cursor-based pagination**, not numeric offset. Prepend of new tasks (own
   and via the banner) shifts every numeric offset boundary, causing duplicates
   or gaps on the next "load more". A cursor keyed on the tail task is immune.
3. **Composite cursor `(created_at, id)`** to survive equal `created_at`
   timestamps (unlikely with manual uploads, but cheap to make correct).
4. **Two cursors, `head` and `tail`.** `tail` (oldest loaded) drives loading
   downward; `head` (newest loaded) drives the "new tasks" detection and the
   upward pull.
5. **Ignore updates below the window.** SSE events for tasks not in the DOM are
   silently ignored (existing early-return). Only events for tasks *newer than
   `head`* matter.
6. **"New tasks ↑" banner** for tasks newer than `head`; click pulls them in via
   an `after` cursor (incremental prepend), preserving the already-loaded tail
   and scroll position.
7. **Prepend own new tasks** on submit/upload, without a full rebuild and
   without moving the cursors.
8. **Configurable page size via BOTH YAML and env**, using the existing config
   loader — no new loading code.

## Section 1 — Backend

### `GET /api/tasks` — add interval cursor params + order

The two cursors are **independent**, forming an open/closed interval
(existing `limit`/`offset`/`compact` untouched):

- `before_ts` + `before_id` — upper bound: tasks strictly older than the cursor.
- `after_ts` + `after_id` — lower bound: tasks strictly newer than the cursor.
- `order` = `desc` (default) | `asc` — which end `LIMIT` cuts from.

Combined predicate (any bound may be absent):
`after < (created_at, id) < before`, i.e. a half-open interval when only one
side is given (single cursor = interval with an open end). `LIMIT pageSize`
applies after ordering.

Client usage:
- **Page downward:** `before = tail`, `order=desc` → newest-first from `tail`
  down; `LIMIT` takes the tasks adjacent to `tail`.
- **Pull newer (banner):** `after = head`, `order=asc` → oldest-first above
  `head`; `LIMIT` takes the tasks **adjacent to `head`** (not the globally
  newest), so prepend stays contiguous with no gap. Client reverses the ASC
  page to DESC for display.

Why `order` is explicit and not derived from which cursor is set: interval-ness
and `LIMIT` direction are orthogonal. With a large delta above `head`,
`after=head` under `desc` would return the *globally newest* tasks and leave a
hole between them and `head`, breaking contiguous prepend. `order=asc` makes
`LIMIT` bite the near edge. Keeping `order` a first-class param also serves the
future filter/MCP work (VOS-84b/c), which needs interval queries in both
directions.

Validation:
- Each cursor pair must be supplied together (both ts and id) → 422 otherwise.
- If both bounds given, require `after < before` → 422 otherwise.
- `order` ∈ {`asc`, `desc`} → 422 otherwise.
- `limit` keeps its existing `0..500` validation.

### Repo

New method alongside `list_tasks_for_user` (which stays as-is for other callers):

```python
async def list_tasks_page(
    self,
    user_id: uuid.UUID,
    *,
    before: tuple[datetime, uuid.UUID] | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
    order: str = "desc",   # "desc" | "asc"
    limit: int,
) -> list[Task]:
    ...
```

- Interval: both bounds independent and optional. Composite comparison via
  SQLAlchemy tuple — add a `WHERE` clause per supplied bound:
  `tuple_(Task.created_at, Task.id) < tuple_(before_ts, before_id)` for `before`,
  `tuple_(Task.created_at, Task.id) > tuple_(after_ts, after_id)` for `after`.
- `order="desc"` → `ORDER BY created_at DESC, id DESC`;
  `order="asc"` → `ORDER BY created_at ASC, id ASC`. `LIMIT` after ordering.
- No mutual-exclusion assert — both bounds may coexist.
- `selectinload(Task.steps)` as the existing method does.

### Config — page size via YAML and env

Add to `Settings` (`vts/core/config.py`):

```python
tasks_page_size: int = 10
```

- **env:** `VTS_TASKS_PAGE_SIZE=10` (works automatically via `env_prefix="VTS_"`).
- **YAML** (`config.yaml`):
  ```yaml
  tasks:
    page_size: 10
  ```
  This flows through the existing `_normalize_yaml_overrides` /
  `_flatten_nested_overrides` machinery: `tasks: { page_size: N }` →
  `tasks_page_size` — the same nesting convention `worker:` → `worker_max_active_tasks`
  already uses. No new loading code.
- **Precedence:** YAML `tasks.page_size` > env `VTS_TASKS_PAGE_SIZE` > field
  default (10). NOTE: in this codebase `get_settings()` loads YAML and passes it
  as `Settings(**overrides)` init-kwargs, and pydantic-settings ranks init-kwargs
  ABOVE env (`settings_customise_sources` only swaps the env source class, it does
  not reorder precedence). So YAML overrides env for every field, this one
  included. Verified empirically 2026-07-30.
- Document in `.env.example` (`# VTS_TASKS_PAGE_SIZE=10`) and `config.yaml`.

### Expose page size to the client

Extend `GET /api/status-config` (`vts/api/main.py:1749`) — which the client
already fetches once at bootstrap — to include `tasks_page_size`:

```json
{"status_flags": {...}, "tasks_page_size": 10}
```

## Section 2 — Client: infinite scroll

Paging state:

```js
state.taskPaging = {
  head: null,        // {ts, id} of newest loaded task
  tail: null,        // {ts, id} of oldest loaded task
  pageSize: 10,      // from /api/status-config, default 10
  loading: false,
  exhausted: false,  // true once a downward page returns < pageSize
  newIds: new Set(), // ids newer than head, awaiting the banner pull
};
```

`renderTasks()` is split so the per-card build (current `forEach` body) becomes
a reusable `appendTaskCard(task)` / `prependTaskCard(task)` helper used by the
first page, downward paging, the banner pull, and own-task prepend.

- **`loadFirstPage()`** — reset: `stopAllLogPolling()`, `taskList.innerHTML=""`,
  clear banner + `newIds`, request `limit=pageSize` with no cursor, append cards,
  set `head` = first card, `tail` = last card, `exhausted = received < pageSize`.
- **`loadNextPage()`** — request `before_ts/before_id` = `tail`, append cards,
  move `tail` to the new last card, set `exhausted` if `received < pageSize`.
  No-op while `loading || exhausted`.

**Trigger:** an `IntersectionObserver` on a sentinel element appended after the
task list. It fires `loadNextPage()` when the sentinel scrolls into view.
IntersectionObserver is available in every browser VTS targets; no scroll-event
fallback. (New sentinel element must precede the `<script>` tag per the
project's DOM-order rule — see memory `feedback_script_dom_order`.)

**"More below" indication (ticket requirement):** the sentinel shows a spinner
while `loading`; when `exhausted` it shows "Больше задач нет" / hides.

## Section 3 — Client: SSE correctness, banner, prepend

### Un-loaded tasks

Keep the existing early-return in `patchTaskStatus` / `patchTaskProgress` /
`patchTaskStep`. An event for a task not in the DOM is silently ignored — no JS
error.

### "New tasks ↑" banner (tasks newer than head)

On a `task_status` event whose `task_id` is not in the DOM:
- The SSE payload carries only `status`/`error`/`queue` (no `created_at`), so
  fetch `GET /api/tasks/{id}` once.
- If `created_at > head.ts` (composite compare vs `head`) → add id to
  `state.taskPaging.newIds`, show/update the banner with the count.
- Otherwise (below `head`, i.e. within or under the loaded window) → ignore. We
  do not care about anything past the bottom edge, regardless of status.

Guard: skip the fetch if the id is already in `newIds` or already in the DOM.

### Banner click → pull newer (incremental prepend)

`loadNewer()`:
- Request `after_ts/after_id` = `head`, `order=asc`, `limit=pageSize` — the
  page is the tasks *adjacent to* `head`, so prepend stays contiguous.
- Reverse the ASC result to DESC, `prependTaskCard` each (newest ends up on
  top), move `head` up to the new topmost card. `tail` untouched — the loaded
  bottom and the scroll position are preserved.
- If `received == pageSize` there may be more newer tasks: recompute `newIds`
  (drop the ones now in the DOM) and leave the banner with the updated count.
  If `received < pageSize`: clear `newIds` and hide the banner.

### Own new tasks (prepend, no rebuild)

Replace the `await loadTasks()` calls on the submit/upload success paths with
`prependTaskCard(task)` + move `head` up. `tail` and the cursor are unaffected
(the cursor points at the tail; prepend never touches it — the core reason
cursor beats offset here). Dedupe by `data-task-id` before inserting.

## Section 4 — Error handling & edge cases

- **Empty list:** first page returns 0 → existing empty-state.
- **SSE reconnect:** `onerror` currently calls `loadTasks()`. Change to
  `loadFirstPage()` — reconnect may have missed events, so reset to a consistent
  state.
- **`actingAs` switch** (admin "act as user"): resets paging via `loadFirstPage()`.
- **Dedupe:** `appendTaskCard` / `prependTaskCard` skip a task whose
  `data-task-id` is already present (guards prepend vs downward-load races).
- **Cursor collisions** on equal `created_at`: removed by the composite
  `(created_at, id)` cursor.
- **`newIds` cleanup:** when a banner-tracked id later shows up in the DOM (via
  `loadNewer`), drop it from the set.

## Section 5 — Testing

**Backend (pytest):**
- `list_tasks_page`: first page (no cursor), downward page via `before`+`order=desc`,
  upward page via `after`+`order=asc`, interval query (`after` and `before`
  together) returns only the in-between tasks, boundary `received < pageSize` →
  caller sees exhaustion, composite cursor correctness when two tasks share
  `created_at`.
- Endpoint: param validation (half a cursor pair → 422, `after >= before` → 422,
  bad `order` → 422, `limit` bounds), `order=asc` returns ASC, `order=desc`
  returns DESC.
- `/api/status-config` includes `tasks_page_size`.

**Config (pytest):**
- `tasks.page_size` from YAML applies.
- `VTS_TASKS_PAGE_SIZE` from env applies over the default; YAML (init-kwarg) wins over env.

**Frontend (verifier-web / Playwright):** `vts/static/*` changes → run
`verifier-web` before tagging a build (project rule).
- First page renders; scrolling the sentinel into view loads the next page.
- Sentinel shows "no more" when exhausted.
- SSE `task_status` for an unknown id newer than `head` shows the banner;
  clicking it prepends the newer task(s).
- SSE event for an id below the window does not show the banner.
- Own-task prepend inserts on top without collapsing the loaded tail.

## Files touched

- `vts/core/config.py` — `tasks_page_size` field.
- `config.yaml`, `.env.example` — document both config paths.
- `vts/db/repo.py` — `list_tasks_page`.
- `vts/api/main.py` — `GET /api/tasks` cursor params; `/api/status-config` page size.
- `vts/static/app.js` — paging state, `loadFirstPage`/`loadNextPage`/`loadNewer`,
  `appendTaskCard`/`prependTaskCard`, IntersectionObserver sentinel, banner,
  SSE handler tweaks, own-task prepend.
- `vts/static/index.html` — sentinel + banner elements (before `<script>`).
- Tests as in Section 5.

## Follow-up issues to file

- **VOS-84b** — Filter by name / date / type (file/url).
- **VOS-84c** — MCP list methods matching the new filter/pagination.
