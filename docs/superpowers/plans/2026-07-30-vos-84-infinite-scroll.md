# VOS-84 Infinite Scroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the load-everything task list with cursor-based infinite scroll, a configurable page size, and correct SSE handling for tasks outside the loaded window (including a "New tasks ↑" banner).

**Architecture:** Backend gains an interval-cursor query on `GET /api/tasks` (`before`/`after` composite `(created_at, id)` bounds + `order`) backed by a new `Repo.list_tasks_page` method; page size is a `Settings` field surfaced through `/api/status-config`. The web client tracks `head`/`tail` cursors, loads pages downward via an IntersectionObserver sentinel, prepends the user's own new tasks, and shows a banner when SSE reports a task newer than `head`.

**Tech Stack:** FastAPI, SQLAlchemy async (Postgres), pydantic-settings (env + YAML), vanilla JS (`vts/static/app.js`), pytest/pytest-asyncio, Playwright (verifier-web).

## Global Constraints

- Bump `vts/__init__.py.__version__` before the final commit (project rule; see memory `feedback_version_bump`).
- Do NOT change existing `limit`/`offset`/`compact` behaviour of `GET /api/tasks` — MCP and ChatGPT Custom Actions depend on it.
- `app.js` has no `defer`; any new element referenced by `getElementById`/`querySelector` at load time must appear in `index.html` BEFORE the `<script src="/static/app.js">` tag at line ~895 (memory `feedback_script_dom_order`). The task list is at `index.html:314`, well before that — safe.
- Repo integration tests run against Postgres via `make_test_engine` from `tests/_db.py` (see `tests/test_presets_repo.py` for the fixture pattern). No SQLite/Postgres split (memory `feedback_test_environment_parity`).
- `vts/static/*` changes → run the `verifier-web` skill before tagging any build.
- Config precedence is YAML > env > field default. In this codebase `get_settings()` passes YAML as `Settings(**overrides)` init-kwargs, which pydantic-settings ranks above env; `settings_customise_sources` only swaps the env source class, it does not reorder. (My original "env > YAML" was wrong; verified empirically 2026-07-30.)

---

## File Structure

- `vts/core/config.py` — add `tasks_page_size: int = 10` field to `Settings`.
- `config.yaml` — document `tasks: { page_size: 10 }`.
- `.env.example` — document `# VTS_TASKS_PAGE_SIZE=10`.
- `vts/db/repo.py` — add `list_tasks_page(...)`; import `tuple_`.
- `vts/api/main.py` — extend `GET /api/tasks` with cursor/order params; add `tasks_page_size` to `/api/status-config`.
- `vts/static/app.js` — paging state, `loadFirstPage`/`loadNextPage`/`loadNewer`, `appendTaskCard`/`prependTaskCard` (extracted from `renderTasks`), IntersectionObserver sentinel wiring, "New tasks" banner, SSE handler tweak, own-task prepend in `createTask`, capture upload response.
- `vts/static/index.html` — sentinel element + "New tasks" banner element (after `#task-list`, before the script tags).
- Tests: `tests/test_list_tasks_page_repo.py` (new), extend `tests/test_config_yaml.py`, `tests/test_config.py`, `tests/test_status_config.py`, and add `tests/test_list_tasks_pagination_api.py` (new).

---

## Task 1: Config — `tasks_page_size` via field + YAML + env

**Files:**
- Modify: `vts/core/config.py` (add field near the other worker/lane ints, ~line 157-168)
- Modify: `config.yaml` (add `tasks:` block near `worker:`/`lane:`, ~line 94)
- Modify: `.env.example`
- Test: `tests/test_config.py`, `tests/test_config_yaml.py`

**Interfaces:**
- Produces: `Settings.tasks_page_size: int` (default 10). YAML key `tasks.page_size` normalizes to `tasks_page_size` through the existing `_flatten_nested_overrides`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config_yaml.py`:

```python
def test_normalize_yaml_overrides_flattens_tasks_page_size() -> None:
    normalized = _normalize_yaml_overrides({"tasks": {"page_size": 25}})
    assert "tasks" not in normalized
    assert normalized["tasks_page_size"] == 25
```

Add to `tests/test_config.py` (env precedence — follow the existing monkeypatch style in that file):

```python
def test_tasks_page_size_defaults_to_10() -> None:
    from vts.core.config import Settings
    assert Settings().tasks_page_size == 10


def test_tasks_page_size_env_overrides_yaml(monkeypatch) -> None:
    from vts.core.config import Settings
    monkeypatch.setenv("VTS_TASKS_PAGE_SIZE", "7")
    # YAML override passed as init kwarg (as _load_yaml_overrides does);
    # env must win.
    assert Settings(tasks_page_size=25).tasks_page_size == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_yaml.py::test_normalize_yaml_overrides_flattens_tasks_page_size tests/test_config.py -k tasks_page_size -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'tasks_page_size'` / KeyError.

- [ ] **Step 3: Add the field**

In `vts/core/config.py`, alongside the other paging/worker ints (after `worker_max_active_tasks: int = 4`, ~line 157):

```python
    tasks_page_size: int = 10
```

`_flatten_nested_overrides` already turns `tasks: { page_size: N }` into `tasks_page_size` with no extra code. Env `VTS_TASKS_PAGE_SIZE` works via `env_prefix="VTS_"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_yaml.py::test_normalize_yaml_overrides_flattens_tasks_page_size tests/test_config.py -k tasks_page_size -v`
Expected: PASS.

- [ ] **Step 5: Document both config paths**

In `config.yaml`, after the `worker:` block (~line 95):

```yaml
tasks:
  page_size: 10        # infinite-scroll page size for the web task list
```

In `.env.example`, add:

```bash
# Web task-list infinite-scroll page size (overrides config.yaml tasks.page_size)
# VTS_TASKS_PAGE_SIZE=10
```

- [ ] **Step 6: Commit**

```bash
git add vts/core/config.py config.yaml .env.example tests/test_config.py tests/test_config_yaml.py
git commit -m "feat(config): add tasks_page_size (YAML + env), default 10 (VOS-84)"
```

---

## Task 2: Repo — `list_tasks_page` interval cursor query

**Files:**
- Modify: `vts/db/repo.py` (add method after `list_tasks_for_user`, ~line 109; extend the `from sqlalchemy import ...` at line 7)
- Test: `tests/test_list_tasks_page_repo.py` (create)

**Interfaces:**
- Consumes: `Settings.tasks_page_size` (not directly — caller passes `limit`).
- Produces:
  ```python
  async def list_tasks_page(
      self,
      user_id: uuid.UUID,
      *,
      before: tuple[datetime, uuid.UUID] | None = None,
      after: tuple[datetime, uuid.UUID] | None = None,
      order: str = "desc",   # "desc" | "asc"
      limit: int,
  ) -> list[Task]
  ```
  Returns tasks for `user_id` within the open interval `after < (created_at, id) < before` (either bound optional), ordered by `(created_at, id)` in `order` direction, capped at `limit`, with `steps` eager-loaded.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_list_tasks_page_repo.py`. Reuse the Postgres session fixture pattern from `tests/test_presets_repo.py` (copy the `session` fixture and `make_test_engine` import verbatim). Helper to insert tasks at controlled timestamps:

```python
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo

from _db import make_test_engine


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed(session, user_id, n):
    """Insert n tasks, task i created at BASE + i minutes (task 0 oldest)."""
    tasks = []
    for i in range(n):
        t = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url=f"https://example.com/{i}",
            status=TaskStatus.completed,
            created_at=BASE + timedelta(minutes=i),
        )
        session.add(t)
        tasks.append(t)
    await session.flush()
    return tasks


@pytest_asyncio.fixture
async def user(session):
    u = User(username="u1")
    session.add(u)
    await session.flush()
    return u


async def test_first_page_desc_returns_newest_first(session, user):
    await _seed(session, user.id, 5)
    repo = Repo(session)
    page = await repo.list_tasks_page(user.id, limit=2)
    assert [t.source_url for t in page] == [
        "https://example.com/4",
        "https://example.com/3",
    ]


async def test_before_pages_downward(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    tail = tasks[3]  # created at minute 3
    page = await repo.list_tasks_page(
        user.id, before=(tail.created_at, tail.id), limit=2
    )
    assert [t.source_url for t in page] == [
        "https://example.com/2",
        "https://example.com/1",
    ]


async def test_after_asc_returns_adjacent_newer(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    head = tasks[1]  # created at minute 1
    page = await repo.list_tasks_page(
        user.id, after=(head.created_at, head.id), order="asc", limit=2
    )
    # ascending, nearest to head first
    assert [t.source_url for t in page] == [
        "https://example.com/2",
        "https://example.com/3",
    ]


async def test_interval_returns_only_in_between(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    lo, hi = tasks[0], tasks[4]
    page = await repo.list_tasks_page(
        user.id,
        after=(lo.created_at, lo.id),
        before=(hi.created_at, hi.id),
        limit=10,
    )
    assert [t.source_url for t in page] == [
        "https://example.com/3",
        "https://example.com/2",
        "https://example.com/1",
    ]


async def test_composite_cursor_breaks_equal_created_at(session, user):
    # Two tasks share created_at; cursor on the larger id must not skip
    # or duplicate the smaller-id one.
    ts = BASE
    a = Task(id=uuid.UUID(int=1), user_id=user.id, source_url="a",
             status=TaskStatus.completed, created_at=ts)
    b = Task(id=uuid.UUID(int=2), user_id=user.id, source_url="b",
             status=TaskStatus.completed, created_at=ts)
    session.add_all([a, b])
    await session.flush()
    repo = Repo(session)
    # desc order → (ts, id=2) first, then (ts, id=1)
    first = await repo.list_tasks_page(user.id, limit=1)
    assert first[0].id == uuid.UUID(int=2)
    nxt = await repo.list_tasks_page(
        user.id, before=(ts, uuid.UUID(int=2)), limit=10
    )
    assert [t.id for t in nxt] == [uuid.UUID(int=1)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_list_tasks_page_repo.py -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'list_tasks_page'`.

- [ ] **Step 3: Implement the method**

In `vts/db/repo.py`, extend the import at line 7:

```python
from sqlalchemy import delete, func, select, tuple_, update
```

Add after `list_tasks_for_user` (~line 109):

```python
    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        limit: int,
    ) -> list[Task]:
        """Cursor page of a user's tasks within the open interval
        ``after < (created_at, id) < before`` (either bound optional).

        Ordered by the composite ``(created_at, id)`` key so equal
        created_at timestamps never cause a skipped or duplicated row.
        ``order`` selects which end ``limit`` cuts from: ``desc`` (newest
        first, for paging downward) or ``asc`` (oldest first, for pulling
        the tasks adjacent to a head cursor).
        """
        key = tuple_(Task.created_at, Task.id)
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id)
        )
        if before is not None:
            stmt = stmt.where(key < tuple_(*before))
        if after is not None:
            stmt = stmt.where(key > tuple_(*after))
        if order == "asc":
            stmt = stmt.order_by(Task.created_at.asc(), Task.id.asc())
        else:
            stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc())
        stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_list_tasks_page_repo.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_list_tasks_page_repo.py
git commit -m "feat(repo): add list_tasks_page interval cursor query (VOS-84)"
```

---

## Task 3: API — cursor params on `GET /api/tasks` + page size in status-config

**Files:**
- Modify: `vts/api/main.py` — `list_tasks` endpoint (~line 2306); `status_config` endpoint (line 1749-1753)
- Test: `tests/test_list_tasks_pagination_api.py` (create); `tests/test_status_config.py` (extend)

**Interfaces:**
- Consumes: `Repo.list_tasks_page` (Task 2); `Settings.tasks_page_size` (Task 1).
- Produces:
  - `GET /api/tasks?before_ts=&before_id=&after_ts=&after_id=&order=desc|asc&limit=` returning `list[TaskOut]`. Existing `limit`/`offset`/`compact` unchanged.
  - `GET /api/status-config` JSON now includes `"tasks_page_size": <int>`.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_status_config.py`:

```python
async def test_status_config_includes_page_size(client) -> None:
    response = await client.get("/api/status-config")
    body = response.json()
    assert body["tasks_page_size"] == 10
```

Create `tests/test_list_tasks_pagination_api.py`. The conftest `client` fixture is authenticated as the seeded user whose id is the module constant `_TEST_USER_ID` (`tests/conftest.py:61`). Seeding uses the `authed_app` fixture's sessionmaker (it yields `(app, factory)`); the same engine backs the dependency override, so rows written via `factory` are visible to the app. Import the id constant from conftest.

```python
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio

from vts.db.models import Task, TaskStatus

from conftest import _TEST_USER_ID


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER_ID = uuid.UUID(_TEST_USER_ID)


async def _seed(factory, n):
    """Insert n tasks for the test user; task i at BASE + i minutes."""
    rows = []
    async with factory() as s:
        for i in range(n):
            t = Task(
                id=uuid.uuid4(),
                user_id=USER_ID,
                source_url=f"https://example.com/{i}",
                status=TaskStatus.completed,
                created_at=BASE + timedelta(minutes=i),
            )
            s.add(t)
            rows.append(t)
        await s.commit()
    return rows


@pytest_asyncio.fixture
def factory(authed_app):
    _app, factory = authed_app
    return factory


async def test_limit_returns_newest_first(client, factory):
    await _seed(factory, 5)
    r = await client.get("/api/tasks?limit=2")
    assert r.status_code == 200
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/4", "https://example.com/3"]


async def test_before_cursor_pages_down(client, factory):
    rows = await _seed(factory, 5)
    tail = rows[3]
    r = await client.get(
        f"/api/tasks?limit=2&before_ts={tail.created_at.isoformat()}"
        f"&before_id={tail.id}"
    )
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/2", "https://example.com/1"]


async def test_after_cursor_order_asc(client, factory):
    rows = await _seed(factory, 5)
    head = rows[1]
    r = await client.get(
        f"/api/tasks?limit=2&order=asc&after_ts={head.created_at.isoformat()}"
        f"&after_id={head.id}"
    )
    urls = [t["source_url"] for t in r.json()]
    assert urls == ["https://example.com/2", "https://example.com/3"]


async def test_half_cursor_pair_is_422(client):
    r = await client.get("/api/tasks?before_ts=2026-01-01T00:00:00+00:00")
    assert r.status_code == 422


async def test_after_ge_before_is_422(client, factory):
    rows = await _seed(factory, 3)
    older, newer = rows[0], rows[2]  # before must be strictly newer than after
    r = await client.get(
        f"/api/tasks?after_ts={newer.created_at.isoformat()}&after_id={newer.id}"
        f"&before_ts={older.created_at.isoformat()}&before_id={older.id}"
    )
    assert r.status_code == 422


async def test_bad_order_is_422(client):
    r = await client.get("/api/tasks?order=sideways")
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_status_config.py::test_status_config_includes_page_size tests/test_list_tasks_pagination_api.py -v`
Expected: FAIL — status-config KeyError; cursor tests 422/wrong order because params don't exist yet (FastAPI ignores unknown query params, so ordering assertions fail rather than 422).

- [ ] **Step 3: Add page size to status-config**

In `vts/api/main.py`, `status_config` (line 1750). It has no `settings` dependency yet — add one and include the field:

```python
    @app.get("/api/status-config")
    async def status_config(
        settings: Settings = Depends(get_settings_dep),
    ) -> JSONResponse:
        """Pure-status semantics for the frontend, fetched once at bootstrap.
        Task-DEPENDENT capabilities ride per-task on TaskOut.capabilities."""
        return JSONResponse(
            {
                "status_flags": _ts.status_flags(),
                "tasks_page_size": settings.tasks_page_size,
            },
            headers=no_cache_headers,
        )
```

- [ ] **Step 4: Add cursor params to `list_tasks`**

Replace the `list_tasks` signature and validation/query block (`vts/api/main.py:2306-2328`). Keep `limit`/`offset`/`compact` and the serialization tail (lines 2329-2342) exactly as they are.

```python
    async def list_tasks(
        limit: int | None = None,
        offset: int = 0,
        compact: bool = False,
        before_ts: datetime | None = None,
        before_id: uuid.UUID | None = None,
        after_ts: datetime | None = None,
        after_id: uuid.UUID | None = None,
        order: str = "desc",
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> list[TaskOut] | list[TaskCompactOut]:
        """List tasks owned by the current user, newest first. Use
        `limit`/`offset` (legacy) or the cursor params
        `before_ts`/`before_id` (older than) and `after_ts`/`after_id`
        (newer than) with `order=desc|asc` to paginate; `compact=true`
        for slim records (ChatGPT Custom Actions cap ~30KB)."""
        if limit is not None and limit < 0:
            raise HTTPException(status_code=422, detail="limit must be non-negative")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        if limit is not None and limit > 500:
            raise HTTPException(status_code=422, detail="limit must be <= 500")
        if order not in ("asc", "desc"):
            raise HTTPException(status_code=422, detail="order must be 'asc' or 'desc'")
        if (before_ts is None) != (before_id is None):
            raise HTTPException(status_code=422, detail="before_ts and before_id must be supplied together")
        if (after_ts is None) != (after_id is None):
            raise HTTPException(status_code=422, detail="after_ts and after_id must be supplied together")
        before = (before_ts, before_id) if before_ts is not None else None
        after = (after_ts, after_id) if after_ts is not None else None
        if before is not None and after is not None and not (after < before):
            raise HTTPException(status_code=422, detail="after cursor must be older than before cursor")
        repo = Repo(session)
        if before is not None or after is not None:
            tasks = await repo.list_tasks_page(
                uuid.UUID(user.id),
                before=before,
                after=after,
                order=order,
                limit=limit if limit is not None else settings.tasks_page_size,
            )
        else:
            tasks = await repo.list_tasks_for_user(
                uuid.UUID(user.id), limit=limit, offset=offset,
            )
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
        task_ids = [task.id for task in tasks]
        asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
        summary_progress = {task.id: summary_progress_for_task(task) for task in tasks}
        if compact:
            return [
                serialize_task_compact(task, queue_positions, asr_progress, summary_progress, lane_positions)
                for task in tasks
            ]
        return [
            serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)
            for task in tasks
        ]
```

Ensure `datetime` is imported at the top of `main.py` (grep first; add `from datetime import datetime` if absent).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_status_config.py tests/test_list_tasks_pagination_api.py -v`
Expected: PASS.

- [ ] **Step 6: Guard against regressions on the legacy path**

Run: `pytest tests/test_uploads_api.py tests/test_task_status.py tests/mcp -v`
Expected: PASS (legacy `limit`/`offset`/`compact` untouched).

- [ ] **Step 7: Commit**

```bash
git add vts/api/main.py tests/test_status_config.py tests/test_list_tasks_pagination_api.py
git commit -m "feat(api): interval cursor params on GET /api/tasks; page size in status-config (VOS-84)"
```

---

## Task 4: Client — extract per-card render into append/prepend helpers

**Files:**
- Modify: `vts/static/app.js` — `renderTasks` (line 1785); the `forEach` body becomes `appendTaskCard(task)`.
- Test: verifier-web (deferred to Task 8; this task is a pure refactor guarded by existing behaviour).

**Interfaces:**
- Produces:
  - `function renderTaskCard(task): HTMLElement` — builds and returns one `.task` root (the current `forEach` body, minus the `taskList` insertion).
  - `function appendTaskCard(task)` — `renderTaskCard` + append to `taskList`, deduped by `data-task-id`.
  - `function prependTaskCard(task)` — `renderTaskCard` + insert as first child of `taskList`, deduped.
  - `renderTasks(tasks)` keeps its signature: clears the list, then `appendTaskCard` for each.

- [ ] **Step 1: Extract `renderTaskCard`**

In `vts/static/app.js`, refactor `renderTasks` (line 1785). Move the entire body inside `tasks.forEach((task) => { ... })` into a new function `renderTaskCard(task)` that returns `root` instead of appending it. The existing final append (search for where `root`/`node` is added to `taskList` inside the loop) becomes the caller's job.

```js
function renderTaskCard(task) {
  const node = taskTemplate.content.cloneNode(true);
  const root = node.querySelector(".task");
  // ... ENTIRE existing per-task body, unchanged ...
  // (do NOT append to taskList here — return the root)
  return root;
}

function appendTaskCard(task) {
  if (findTaskEl(task.id)) return;          // dedupe
  taskList.appendChild(renderTaskCard(task));
}

function prependTaskCard(task) {
  if (findTaskEl(task.id)) return;          // dedupe
  taskList.insertBefore(renderTaskCard(task), taskList.firstChild);
}

function renderTasks(tasks) {
  stopAllLogPolling();
  taskList.innerHTML = "";
  tasks.forEach((task) => appendTaskCard(task));
}
```

`findTaskEl` already exists (`app.js:2862`). Preserve every side effect in the current loop body (event listeners, `_runtime` assignment, `renderTaskRuntime`, etc.) — this is a mechanical extraction, not a rewrite.

- [ ] **Step 2: Manual sanity — load the app**

Run the app per the `run` skill (or existing dev flow) and confirm the task list still renders identically: cards appear, expand, and SSE updates still patch. No behavioural change expected.

- [ ] **Step 3: Commit**

```bash
git add vts/static/app.js
git commit -m "refactor(app.js): extract renderTaskCard + append/prepend helpers (VOS-84)"
```

---

## Task 5: Client — paging state, first/next page, IntersectionObserver sentinel

**Files:**
- Modify: `vts/static/index.html` — add sentinel after `#task-list` (line 314), before script tags.
- Modify: `vts/static/app.js` — paging state; `loadFirstPage`/`loadNextPage`; replace `loadTasks` body; observer wiring; bootstrap picks up `tasks_page_size`.
- Test: verifier-web (Task 8).

**Interfaces:**
- Consumes: `/api/status-config` `tasks_page_size` (Task 3); `appendTaskCard` (Task 4); `GET /api/tasks?before_ts&before_id&limit&order` (Task 3).
- Produces:
  - `state.taskPaging = { head, tail, pageSize, loading, exhausted, newIds }`.
  - `async function loadFirstPage()` — reset + load newest page, set `head`/`tail`.
  - `async function loadNextPage()` — load older page via `before=tail`, append.
  - `function cursorOf(taskEl)` → `{ts, id}` from a card's `_runtime`/dataset.
  - `loadTasks` becomes an alias for `loadFirstPage` (so the 8 existing callers behave as "reset to first page").

- [ ] **Step 1: Add the sentinel element**

In `vts/static/index.html`, immediately after `<div id="task-list" class="task-list"></div>` (line 314):

```html
        <div id="task-sentinel" class="task-sentinel" hidden>
          <span class="task-sentinel-spinner" aria-hidden="true"></span>
          <span class="task-sentinel-end" data-i18n="tasks.no_more" hidden></span>
        </div>
```

Add an i18n key `tasks.no_more` ("Больше задач нет" / "No more tasks") to the client i18n table (search `app.js` for an existing key like `tab.prompt_transcript` to find the table; follow its shape). If i18n lookup for a missing key already degrades gracefully, still add the key for both locales.

- [ ] **Step 2: Add paging state and page-size capture**

Near the top-of-file `state` object in `app.js`, add:

```js
state.taskPaging = {
  head: null, tail: null, pageSize: 10,
  loading: false, exhausted: false, newIds: new Set(),
};
```

In the bootstrap where `/api/status-config` is fetched (`app.js:3403-3405`), capture the size:

```js
    const cfg = await api("/api/status-config");
    if (cfg && cfg.status_flags) window.statusPred.setFlags(cfg.status_flags);
    if (cfg && Number.isFinite(cfg.tasks_page_size)) {
      state.taskPaging.pageSize = cfg.tasks_page_size;
    }
```

- [ ] **Step 3: Implement `cursorOf`, `loadFirstPage`, `loadNextPage`**

`renderTaskCard` stores the task's `created_at`; ensure it sets `root.dataset.createdAt = task.created_at` (add this line next to the existing `root.dataset.taskId = task.id`). Then:

```js
function cursorOf(taskEl) {
  if (!taskEl) return null;
  return { ts: taskEl.dataset.createdAt, id: taskEl.dataset.taskId };
}

function updateHeadTail() {
  const cards = taskList.querySelectorAll(".task");
  state.taskPaging.head = cursorOf(cards[0]);
  state.taskPaging.tail = cursorOf(cards[cards.length - 1]);
}

function updateSentinel() {
  const sentinel = document.getElementById("task-sentinel");
  if (!sentinel) return;
  const p = state.taskPaging;
  sentinel.hidden = false;
  sentinel.querySelector(".task-sentinel-spinner").hidden = !p.loading;
  sentinel.querySelector(".task-sentinel-end").hidden = !p.exhausted;
}

async function loadFirstPage() {
  const p = state.taskPaging;
  p.loading = true;
  clearNewTasksBanner();          // defined in Task 6; safe no-op if not yet present
  updateSentinel();
  let tasks;
  try {
    tasks = await api(`/api/tasks?limit=${p.pageSize}`);
  } catch (err) {
    taskList.textContent = err.message;
    p.loading = false;
    return;
  }
  renderTasks(tasks);
  p.exhausted = tasks.length < p.pageSize;
  updateHeadTail();
  p.loading = false;
  updateSentinel();
}

async function loadNextPage() {
  const p = state.taskPaging;
  if (p.loading || p.exhausted || !p.tail) return;
  p.loading = true;
  updateSentinel();
  const q = new URLSearchParams({
    limit: String(p.pageSize),
    order: "desc",
    before_ts: p.tail.ts,
    before_id: p.tail.id,
  });
  let tasks;
  try {
    tasks = await api(`/api/tasks?${q.toString()}`);
  } catch {
    p.loading = false;
    updateSentinel();
    return;
  }
  tasks.forEach((t) => appendTaskCard(t));
  p.exhausted = tasks.length < p.pageSize;
  updateHeadTail();
  p.loading = false;
  updateSentinel();
}
```

Replace the old `loadTasks` body (`app.js:2025-2031`) so every existing caller resets to the first page:

```js
async function loadTasks() {
  await loadFirstPage();
}
```

- [ ] **Step 4: Wire the IntersectionObserver**

After the app's initial load wiring (near where `connectEvents()` is first called, `app.js:3406`), observe the sentinel:

```js
const taskSentinelObserver = new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) void loadNextPage();
}, { rootMargin: "200px" });
const _sentinelEl = document.getElementById("task-sentinel");
if (_sentinelEl) taskSentinelObserver.observe(_sentinelEl);
```

- [ ] **Step 5: Manual verification**

With page size set low (e.g. `VTS_TASKS_PAGE_SIZE=3`) and >3 tasks seeded: first load shows 3, scrolling to the bottom loads the next 3, and when fewer than 3 remain the sentinel shows "no more". Verify no duplicate cards.

- [ ] **Step 6: Commit**

```bash
git add vts/static/index.html vts/static/app.js
git commit -m "feat(app.js): cursor-based infinite scroll via IntersectionObserver sentinel (VOS-84)"
```

---

## Task 6: Client — "New tasks ↑" banner + SSE detection + banner pull

**Files:**
- Modify: `vts/static/index.html` — banner element above `#task-list`.
- Modify: `vts/static/app.js` — banner helpers; `task_status` SSE handler (`app.js:3255-3258`); `loadNewer`.
- Test: verifier-web (Task 8).

**Interfaces:**
- Consumes: `state.taskPaging.head`/`newIds` (Task 5); `GET /api/tasks?after_ts&after_id&order=asc&limit` (Task 3); `GET /api/tasks/{id}`; `prependTaskCard` (Task 4).
- Produces:
  - `function clearNewTasksBanner()` (referenced in Task 5 — define here; forward-safe because Task 5's call is guarded/late).
  - `function showNewTasksBanner()`.
  - `async function maybeFlagNewerTask(taskId)` — fetch task, compare to `head`, add to `newIds` + show banner if newer.
  - `async function loadNewer()` — banner click handler; pull one page above `head`, prepend, recount.

- [ ] **Step 1: Add the banner element**

In `vts/static/index.html`, immediately BEFORE `<div id="task-list" ...>` (line 314):

```html
        <button id="new-tasks-banner" class="new-tasks-banner" type="button" hidden>
          <span data-i18n="tasks.new_above">Новые задачи</span>
          <span id="new-tasks-count" class="new-tasks-count"></span>
        </button>
```

Add i18n key `tasks.new_above` for both locales ("Новые задачи ↑" / "New tasks ↑").

- [ ] **Step 2: Implement banner helpers and detection**

Composite compare helper and banner logic in `app.js`:

```js
function isNewerThan(ts, id, cursor) {
  // returns true if (ts,id) > cursor lexicographically on (ts, id)
  if (!cursor) return true;
  if (ts > cursor.ts) return true;
  if (ts < cursor.ts) return false;
  return String(id) > String(cursor.id);
}

function clearNewTasksBanner() {
  state.taskPaging.newIds.clear();
  const b = document.getElementById("new-tasks-banner");
  if (b) b.hidden = true;
}

function showNewTasksBanner() {
  const b = document.getElementById("new-tasks-banner");
  const c = document.getElementById("new-tasks-count");
  if (!b) return;
  const n = state.taskPaging.newIds.size;
  if (n === 0) { b.hidden = true; return; }
  if (c) c.textContent = `(${n})`;
  b.hidden = false;
}

async function maybeFlagNewerTask(taskId) {
  const p = state.taskPaging;
  if (p.newIds.has(taskId) || findTaskEl(taskId)) return;
  let task;
  try {
    task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch {
    return;
  }
  if (!task || !task.created_at) return;
  if (isNewerThan(task.created_at, task.id, p.head)) {
    p.newIds.add(taskId);
    showNewTasksBanner();
  }
}
```

- [ ] **Step 3: Hook the `task_status` SSE handler**

At `app.js:3255-3258`, after the existing `patchTaskStatus(...)` call, flag unknown-newer tasks:

```js
  state.eventSource.addEventListener("task_status", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStatus(payload.task_id, payload.data.status, payload.data.error, payload.data.failure_code, payload.data.queue, payload.data.awaiting_step);
    if (!findTaskEl(payload.task_id)) {
      void maybeFlagNewerTask(payload.task_id);
    }
  });
```

(`patchTaskStatus` already early-returns for unknown ids, so this adds only the banner path.)

- [ ] **Step 4: Implement `loadNewer` and bind the click**

```js
async function loadNewer() {
  const p = state.taskPaging;
  if (p.loading || !p.head) return;
  p.loading = true;
  const q = new URLSearchParams({
    limit: String(p.pageSize),
    order: "asc",
    after_ts: p.head.ts,
    after_id: p.head.id,
  });
  let tasks;
  try {
    tasks = await api(`/api/tasks?${q.toString()}`);
  } catch {
    p.loading = false;
    return;
  }
  // ASC from server → reverse so newest ends on top after successive prepends
  tasks.slice().reverse().forEach((t) => prependTaskCard(t));
  updateHeadTail();
  // Drop now-loaded ids; if a full page came back there may be more above.
  tasks.forEach((t) => p.newIds.delete(t.id));
  if (tasks.length < p.pageSize) {
    clearNewTasksBanner();
  } else {
    showNewTasksBanner();
  }
  p.loading = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.getElementById("new-tasks-banner")
  ?.addEventListener("click", () => void loadNewer());
```

- [ ] **Step 5: Manual verification**

- With two browser sessions (or the admin "act as"), create a task in session B while session A is scrolled; session A shows the "New tasks (1)" banner; clicking it prepends the task and clears the banner.
- Confirm a `task_status` event for an id already below the loaded window does NOT show the banner (seed many tasks, page down, then trigger a status change on an old task).

- [ ] **Step 6: Commit**

```bash
git add vts/static/index.html vts/static/app.js
git commit -m "feat(app.js): New tasks banner + SSE newer-than-head detection + pull (VOS-84)"
```

---

## Task 7: Client — prepend own new tasks; SSE reconnect resets to first page

**Files:**
- Modify: `vts/static/app.js` — `createTask` (`app.js:2660-2720`); `uploadFileWithProgress` (`app.js:2058`) to resolve with the parsed response; SSE `onerror` (`app.js:3312-3321`).
- Test: verifier-web (Task 8).

**Interfaces:**
- Consumes: `prependTaskCard` (Task 4); `updateHeadTail` (Task 5).
- Produces: own-created task appears on top immediately (no full rebuild, no banner for one's own task).

- [ ] **Step 1: Make `uploadFileWithProgress` resolve with the created task**

At `app.js:2058`, the `xhr.onload` success branch currently does `resolve();`. Parse and return the task JSON:

```js
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        let task = null;
        try { task = JSON.parse(xhr.responseText); } catch (_) {}
        resolve(task);
      } else {
        let msg = `HTTP ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText)?.detail || msg; } catch (_) {}
        reject(new Error(msg));
      }
    };
```

- [ ] **Step 2: Capture responses and prepend in `createTask`**

In `createTask` (`app.js:2660`), the two create branches currently discard their results. Capture them:

```js
        const created = await uploadFileWithProgress(fd);   // file branch
```
```js
        const created = await api("/api/tasks", {            // url branch
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
```

Hoist a `let created = null;` above the `try` so it is visible after it. Then replace the trailing `await loadTasks();` (line 2720) with a prepend that falls back to a full reset if the response was somehow empty:

```js
  form.reset();
  form.transcript.checked = true;
  resetPromptSelection();
  syncSummaryToggle();
  syncSourceType();
  if (created && created.id) {
    prependTaskCard(created);
    updateHeadTail();
    void refreshQueuePositions();
  } else {
    await loadFirstPage();
  }
```

Rationale: the server also emits a `task_status` SSE event for this new task, but since we prepend it here it is already in the DOM, so `maybeFlagNewerTask` skips it (its `findTaskEl` guard) — the user's own task never trips the banner.

- [ ] **Step 3: SSE reconnect → first page**

At `app.js:3319`, the `onerror` reconnect currently calls `void loadTasks();`. It now aliases to `loadFirstPage()`, which is the desired reset-on-reconnect. Confirm the line reads:

```js
    setTimeout(() => {
      connectEvents();
      void loadFirstPage();
    }, 2000);
```

(Change `loadTasks` → `loadFirstPage` explicitly for clarity even though they are aliased.)

- [ ] **Step 4: Manual verification**

- Submit a URL task → it appears on top instantly, no full-list flicker, scroll position of the rest preserved.
- Upload a file task → same.
- Kill Redis / drop the SSE connection briefly → on reconnect the list resets to the first page cleanly.

- [ ] **Step 5: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(app.js): prepend own new tasks; reset to first page on SSE reconnect (VOS-84)"
```

---

## Task 8: Browser verification + version bump

**Files:**
- Modify: `vts/__init__.py` (version bump)
- Verify: `vts/static/*` via verifier-web skill.

- [ ] **Step 1: Run the full backend test suite**

Run: `pytest tests/ -q`
Expected: PASS (all, including the new repo/api/config tests and untouched legacy tests).

- [ ] **Step 2: Browser-verify the frontend**

Invoke the `verifier-web` skill. Verify against stubbed `/api/*`:
- First page renders `pageSize` cards; sentinel visible.
- Scrolling the sentinel into view requests `before_ts/before_id` and appends the next page; sentinel shows "no more" when the stub returns `< pageSize`.
- A stubbed `task_status` SSE event for an unknown id whose `GET /api/tasks/{id}` is newer than head shows the "New tasks" banner; clicking it issues an `after_ts/after_id&order=asc` request and prepends.
- An SSE event for an id older than head does not show the banner.
- Submitting a task prepends one card without clearing the list.

Fix any failures (loop back to the relevant task) before proceeding.

- [ ] **Step 3: Bump version**

Edit `vts/__init__.py` — increment `__version__` (patch bump) per project rule.

- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py
git commit -m "chore: bump version for VOS-84 infinite scroll"
```

- [ ] **Step 5: Push**

```bash
git pull --rebase
git push
git status   # must show up to date with origin
```

---

## Self-Review

**Spec coverage:**
- Infinite scroll + configurable page size → Tasks 1, 3, 5. ✓
- Cursor pagination (composite `head`/`tail`) → Tasks 2, 3, 5. ✓
- "More below" indication → Task 5 (sentinel spinner / "no more"). ✓
- SSE ignore of un-loaded tasks → unchanged early-return, noted in Task 6. ✓
- "New tasks ↑" banner (newer-than-head) + pull via `after` → Task 6. ✓
- Prepend own new tasks, no rebuild → Task 7. ✓
- Config via YAML AND env, precedence YAML>env>default (codebase reality) → Task 1. ✓
- Page size to client via status-config → Tasks 3, 5. ✓
- SSE reconnect resets consistently → Task 7. ✓
- `actingAs` switch resets paging → handled: those paths call `loadTasks` which now aliases `loadFirstPage`. ✓
- Tests (repo/api/config/frontend) → Tasks 2, 3, 1, 8. ✓

**Placeholder scan:** No TBD/TODO; all code steps carry concrete code. Task 3's API tests seed via the real conftest fixtures (`client`, `authed_app`→`factory`, `_TEST_USER_ID`), verified against `tests/conftest.py:51-124` — no invented fixtures.

**Type consistency:** `list_tasks_page(before, after, order, limit)` signature identical in Tasks 2 and 3. `head`/`tail` are `{ts, id}` throughout (Tasks 5, 6, 7). `appendTaskCard`/`prependTaskCard`/`renderTaskCard`/`cursorOf`/`updateHeadTail`/`clearNewTasksBanner` names consistent across Tasks 4–7. `before_ts/before_id/after_ts/after_id/order` query params identical in API (Task 3) and client (Tasks 5, 6).

**Known follow-ups (out of scope, file as issues):** VOS-84b filter (name/date/type); VOS-84c MCP list methods.
