# vts-kdc — MCP `list_tasks` Cursor Pagination Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Execute task-by-task with tests between.

**Goal:** Add opaque-cursor pagination (created_at DESC) + optional status filter to the MCP `list_tasks` tool, so MCP clients can page through all their tasks.

**Architecture:** Reuse VOS-84's `Repo.list_tasks_page` composite `(created_at, id)` cursor; add a `status` filter to it. A small `vts/mcp/cursor.py` codec makes the cursor opaque. The MCP tool returns a `TaskPage` with `next_cursor`/`has_more` and drops `sort`/`order` (agreed breaking change).

**Tech Stack:** FastMCP, SQLAlchemy async (Postgres), pydantic, pytest.

## Global Constraints

- Bump `vts/__init__.py.__version__` before the final commit (already at 1.5.27 on this branch from vts-7ud; bump to 1.5.28).
- Do NOT change the web `/api/tasks` endpoint or its behavior. `list_tasks_page`'s new `status` param defaults to `None`; the endpoint never passes it.
- Repo tests run against Postgres via `make_test_engine` (`tests/_db.py`). No SQLite split.
- MCP tool unit tests use the in-repo `FakeRepo`/`FakeUser`/`FakeTask` from `tests/mcp/conftest.py`.
- Python: `/home/victor/dev/vts/.venv/bin/python -m pytest ...` (no `python` on PATH).
- Breaking change to the MCP tool schema is intended: existing tests asserting the old `sort`/`order`/`list` shape MUST be rewritten to the new `cursor`/`TaskPage` contract, not weakened.

---

## Task 1: Repo — add `status` filter to `list_tasks_page`

**Files:**
- Modify: `vts/db/repo.py` (`list_tasks_page`, ~line 111)
- Test: `tests/test_list_tasks_page_repo.py` (extend)

**Interfaces:**
- Produces: `list_tasks_page(..., status: TaskStatus | None = None)` — adds a status predicate; cursor/order semantics unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_list_tasks_page_repo.py` (reuse its `session`/`user`/`_seed`/`BASE` helpers; `TaskStatus` is imported there):

```python
async def test_status_filter_narrows_and_cursor_still_pages(session, user):
    # 4 tasks: alternate completed/queued, minute 0..3 (task 3 newest).
    from datetime import timedelta
    import uuid as _uuid
    from vts.db.models import Task
    rows = []
    for i in range(4):
        t = Task(
            id=_uuid.uuid4(), user_id=user.id, source_url=f"https://example.com/{i}",
            artifact_dir=f"/tmp/{i}",
            status=TaskStatus.completed if i % 2 == 0 else TaskStatus.queued,
            created_at=BASE + timedelta(minutes=i),
        )
        session.add(t); rows.append(t)
    await session.flush()
    repo = Repo(session)
    # completed tasks are i=0 (min0) and i=2 (min2); newest-first → [2, 0]
    page = await repo.list_tasks_page(user.id, limit=1, status=TaskStatus.completed)
    assert [t.source_url for t in page] == ["https://example.com/2"]
    tail = page[-1]
    page2 = await repo.list_tasks_page(
        user.id, before=(tail.created_at, tail.id), limit=10, status=TaskStatus.completed
    )
    assert [t.source_url for t in page2] == ["https://example.com/0"]
    # no queued leaked in either page
    assert all("example.com/1" not in t.source_url and "example.com/3" not in t.source_url
               for t in page + page2)
```

- [ ] **Step 2: Run — expect FAIL** (`TypeError: unexpected keyword 'status'`)

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_list_tasks_page_repo.py::test_status_filter_narrows_and_cursor_still_pages -v`

- [ ] **Step 3: Implement**

In `vts/db/repo.py`, `list_tasks_page` — add the param and one clause:

```python
    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        limit: int,
        status: TaskStatus | None = None,
    ) -> list[Task]:
```

After the `.where(Task.user_id == user_id)` line (before the before/after clauses is fine; anywhere in the WHERE chain works):

```python
        if status is not None:
            stmt = stmt.where(Task.status == status)
```

`TaskStatus` is already imported in repo.py. Update the docstring's first line to mention the optional status filter.

- [ ] **Step 4: Run — expect PASS**, plus the whole file green:

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_list_tasks_page_repo.py -v`

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_list_tasks_page_repo.py
git commit -m "feat(repo): optional status filter on list_tasks_page (vts-kdc)"
```

---

## Task 2: Cursor codec

**Files:**
- Create: `vts/mcp/cursor.py`
- Test: `tests/test_mcp_cursor.py` (create)

**Interfaces:**
- Produces:
  - `encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str`
  - `decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]` (raises `ValueError` on bad input)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_cursor.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from vts.mcp.cursor import encode_cursor, decode_cursor


def test_round_trip_preserves_created_at_and_id():
    ts = datetime(2026, 7, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)
    tid = uuid.uuid4()
    token = encode_cursor(ts, tid)
    assert isinstance(token, str) and token
    got_ts, got_id = decode_cursor(token)
    assert got_ts == ts
    assert got_id == tid


def test_round_trip_zero_microseconds():
    ts = datetime(2026, 7, 30, 12, 34, 56, 0, tzinfo=timezone.utc)
    tid = uuid.uuid4()
    got_ts, got_id = decode_cursor(encode_cursor(ts, tid))
    assert got_ts == ts and got_id == tid


@pytest.mark.parametrize("bad", [
    "not-base64-!!!",
    "",
    "YWJjZGVm",            # valid base64 but no '|' separator
])
def test_decode_rejects_malformed(bad):
    with pytest.raises(ValueError):
        decode_cursor(bad)


def test_decode_rejects_bad_datetime_and_uuid():
    import base64
    raw = base64.urlsafe_b64encode(b"not-a-date|not-a-uuid").decode().rstrip("=")
    with pytest.raises(ValueError):
        decode_cursor(raw)
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: vts.mcp.cursor`)

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_mcp_cursor.py -v`

- [ ] **Step 3: Implement**

Create `vts/mcp/cursor.py`:

```python
from __future__ import annotations

import base64
import uuid
from datetime import datetime

# Opaque pagination cursor for the MCP list_tasks tool. Encodes the composite
# (created_at, id) key that Repo.list_tasks_page pages on. Clients treat the
# string as opaque — they echo back the next_cursor from the previous page.

_SEP = "|"


def encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}{_SEP}{task_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    """Decode an opaque cursor to (created_at, task_id).

    Raises ValueError on any malformation (bad base64, missing separator,
    unparseable datetime or uuid).
    """
    if not raw:
        raise ValueError("empty cursor")
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
    except Exception as exc:  # binascii.Error, UnicodeDecodeError
        raise ValueError("cursor is not valid base64") from exc
    if _SEP not in decoded:
        raise ValueError("cursor missing separator")
    ts_str, _, id_str = decoded.partition(_SEP)
    try:
        created_at = datetime.fromisoformat(ts_str)
    except ValueError as exc:
        raise ValueError("cursor has invalid datetime") from exc
    try:
        task_id = uuid.UUID(id_str)
    except ValueError as exc:
        raise ValueError("cursor has invalid uuid") from exc
    return created_at, task_id
```

- [ ] **Step 4: Run — expect PASS**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_mcp_cursor.py -v`

- [ ] **Step 5: Commit**

```bash
git add vts/mcp/cursor.py tests/test_mcp_cursor.py
git commit -m "feat(mcp): opaque cursor codec for task pagination (vts-kdc)"
```

---

## Task 3: `TaskPage` schema + reworked `list_tasks` tool + FakeRepo

**Files:**
- Modify: `vts/mcp/schemas.py` (add `TaskPage`)
- Modify: `vts/mcp/tools.py` (`list_tasks` rework; `_RepoListLike` protocol)
- Modify: `tests/mcp/conftest.py` (add `list_tasks_page` to `FakeRepo`)
- Test: `tests/mcp/test_tools_list.py` (rewrite)

**Interfaces:**
- Consumes: `Repo.list_tasks_page(..., status=...)` (Task 1); `encode_cursor`/`decode_cursor` (Task 2).
- Produces:
  - `TaskPage(tasks: list[TaskSummary], next_cursor: str | None, has_more: bool)`
  - `list_tasks(*, user, repo, status=None, limit=20, cursor=None) -> TaskPage`

- [ ] **Step 1: Add `TaskPage` to `vts/mcp/schemas.py`**

After the `TaskSummary` class:

```python
class TaskPage(BaseModel):
    tasks: list[TaskSummary]
    next_cursor: str | None = None
    has_more: bool = False
```

- [ ] **Step 2: Add `list_tasks_page` to `FakeRepo` in `tests/mcp/conftest.py`**

After `list_tasks_for_user_filtered` (~line 196), add a fake mirroring the real cursor semantics (composite (created_at, id), newest-first for order="desc", optional status, optional `before` bound):

```python
    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple = None,
        after: tuple = None,
        order: str = "desc",
        limit: int = 20,
        status: str | None = None,
    ) -> list[FakeTask]:
        items = [t for t in self.tasks.values() if t.user_id == user_id]
        if status is not None:
            items = [t for t in items if t.status == status]
        items.sort(key=lambda t: (t.created_at, str(t.id)), reverse=(order == "desc"))
        if before is not None:
            b_ts, b_id = before
            items = [t for t in items if (t.created_at, str(t.id)) < (b_ts, str(b_id))]
        if after is not None:
            a_ts, a_id = after
            items = [t for t in items if (t.created_at, str(t.id)) > (a_ts, str(a_id))]
        return items[:limit]
```

(Note the real repo's `status` is a `TaskStatus` enum; `FakeTask.status` is a str. The tool passes the enum to the real repo but the fake compares by str — so in the fake, accept the value and compare loosely. If the tool maps the literal to a `TaskStatus` enum before calling repo, the fake's `t.status == status` would compare str to enum and fail. To keep the fake honest, compare `str(status) ...`; simplest: have the fake filter with `t.status == getattr(status, "value", status)`.)

Adjust the status line to:
```python
        if status is not None:
            want = getattr(status, "value", status)
            items = [t for t in items if t.status == want]
```

- [ ] **Step 3: Rewrite `tests/mcp/test_tools_list.py` (the failing tests)**

Replace the file's tests with the new contract:

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import list_tasks
from vts.mcp.schemas import TaskPage


def _seed(repo: FakeRepo, user_id: uuid.UUID, n: int) -> list[FakeTask]:
    base = datetime(2026, 7, 30, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        t = FakeTask(
            id=uuid.uuid4(), user_id=user_id, source_url=f"https://x/{i}",
            source_title=f"title-{i}",
            status="completed" if i % 2 == 0 else "running",
            created_at=base + timedelta(minutes=i),   # task n-1 is newest
            updated_at=base + timedelta(minutes=i),
        )
        repo.tasks[t.id] = t
        out.append(t)
    return out


@pytest.mark.asyncio
async def test_first_page_newest_first_with_cursor_when_more():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    seeded = _seed(repo, uuid.UUID(user.id), 5)
    page = await list_tasks(user=user, repo=repo, limit=2)
    assert isinstance(page, TaskPage)
    # newest-first: minutes 4,3
    assert [str(t.url) for t in page.tasks] == ["https://x/4", "https://x/3"]
    assert page.has_more is True
    assert page.next_cursor is not None


@pytest.mark.asyncio
async def test_second_page_via_cursor_no_overlap():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 5)
    p1 = await list_tasks(user=user, repo=repo, limit=2)
    p2 = await list_tasks(user=user, repo=repo, limit=2, cursor=p1.next_cursor)
    assert [t.url for t in p2.tasks] == ["https://x/2", "https://x/1"]
    assert p2.has_more is True
    # no overlap
    assert set(t.task_id for t in p1.tasks).isdisjoint(t.task_id for t in p2.tasks)


@pytest.mark.asyncio
async def test_last_page_has_no_cursor():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 3)
    page = await list_tasks(user=user, repo=repo, limit=10)
    assert len(page.tasks) == 3
    assert page.has_more is False
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_status_filter():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 4)   # completed at i=0,2
    page = await list_tasks(user=user, repo=repo, status="completed", limit=10)
    assert all(t.status == "completed" for t in page.tasks)
    assert len(page.tasks) == 2


@pytest.mark.asyncio
async def test_invalid_cursor_is_422():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, cursor="!!!not-a-cursor!!!")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_limit_out_of_range_is_422():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, limit=999)
    assert exc.value.status_code == 422
```

- [ ] **Step 4: Run — expect FAIL** (tool still has old signature)

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/mcp/test_tools_list.py -v`

- [ ] **Step 5: Rework `list_tasks` in `vts/mcp/tools.py`**

Update the `_RepoListLike` protocol (replace the `list_tasks_for_user_filtered` declaration with `list_tasks_page`):

```python
class _RepoListLike(Protocol):
    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        limit: int = 20,
        status: Any = None,
    ) -> list[Any]: ...
```

Replace the `list_tasks` function:

```python
async def list_tasks(
    *,
    user: _UserLike,
    repo: _RepoListLike,
    status: Literal["queued", "running", "waiting", "paused", "completed", "archived", "failed", "canceled"] | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> TaskPage:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    before = None
    if cursor:
        try:
            before = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid cursor")
    status_enum = TaskStatus(status) if status is not None else None
    tasks = await repo.list_tasks_page(
        uuid.UUID(user.id), before=before, order="desc", limit=limit, status=status_enum,
    )
    summaries = [
        TaskSummary(
            task_id=t.id, status=t.status, title=t.source_title,
            url=t.source_url, created_at=t.created_at, updated_at=t.updated_at,
        )
        for t in tasks
    ]
    has_more = len(tasks) == limit
    next_cursor = encode_cursor(tasks[-1].created_at, tasks[-1].id) if (has_more and tasks) else None
    return TaskPage(tasks=summaries, next_cursor=next_cursor, has_more=has_more)
```

Add imports at the top of tools.py: `from vts.mcp.cursor import encode_cursor, decode_cursor`, `from vts.mcp.schemas import TaskPage` (extend the existing schemas import), `from vts.db.models import TaskStatus`, and `from datetime import datetime` if not present. Confirm `Literal`, `Any`, `Protocol`, `HTTPException`, `TaskStatus` availability (grep first).

NOTE on `status_enum`: the real `TaskStatus(status)` maps the literal string to the enum. The `FakeRepo` compares via `getattr(status, "value", status)` → the enum's `.value` (the same string), so both real and fake agree.

- [ ] **Step 6: Run — expect PASS**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/mcp/test_tools_list.py tests/test_mcp_cursor.py -v`

- [ ] **Step 7: Commit**

```bash
git add vts/mcp/schemas.py vts/mcp/tools.py tests/mcp/conftest.py tests/mcp/test_tools_list.py
git commit -m "feat(mcp): paginate list_tasks tool with opaque cursor + TaskPage (vts-kdc)"
```

---

## Task 4: FastMCP wrapper + integration test + version bump

**Files:**
- Modify: `vts/mcp/server.py` (`_list_tasks` wrapper, ~line 137)
- Modify: `tests/mcp/test_server_integration.py` (update to `TaskPage` shape)
- Modify: `vts/__init__.py` (version bump)

**Interfaces:**
- Consumes: `list_tasks` (Task 3) returning `TaskPage`.

- [ ] **Step 1: Update the FastMCP wrapper in `vts/mcp/server.py`**

```python
    @mcp.tool(name="list_tasks")
    async def _list_tasks(
        status: Literal[
            "queued", "running", "paused", "completed", "archived", "failed", "canceled"
        ] | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> TaskPage:
        """List the calling user's tasks, newest first, in pages.

        Returns up to `limit` tasks (max 100) plus `next_cursor` and `has_more`.
        To fetch the next page, call again with `cursor` set to the
        `next_cursor` from the previous response. When `has_more` is false (or
        `next_cursor` is null) there are no more tasks. Optionally filter by
        `status`.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, cursor=cursor,
            )
```

Ensure `TaskPage` is imported in server.py (it imports schemas already — extend that import; grep for the existing `from vts.mcp.schemas import` line).

- [ ] **Step 2: Update `tests/mcp/test_server_integration.py`**

The smoke test at line ~62 calls `client.call_tool("list_tasks", {})` and asserts a `list`. Update it to the `TaskPage` structured shape. Read the current assertion, then change it to assert the result carries a `tasks` list (and `has_more`/`next_cursor` keys). Keep the rest of the integration wiring. Concretely, the structured content will now be a dict with `tasks`, `next_cursor`, `has_more` — assert `result.data` (or the parsed structured content) has a `tasks` list and boolean `has_more`. Match whatever access pattern the existing test uses for structured content; do not invent a new one.

- [ ] **Step 3: Run the whole MCP suite + integration**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/mcp/ tests/test_mcp_cursor.py tests/test_list_tasks_page_repo.py -v`
Expected: all PASS. Fix any fallout (e.g. `test_server_tools_registered.py` should still pass since the name is unchanged; `test_schemas.py` unaffected).

- [ ] **Step 4: Confirm web endpoint + full suite unaffected**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_list_tasks_pagination_api.py tests/test_openapi_spec.py -q`
Then the full suite: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/ -q --ignore=tests/ui`
Expected: all PASS.

- [ ] **Step 5: Version bump**

Edit `vts/__init__.py`: `__version__ = "1.5.28"`.

- [ ] **Step 6: Commit**

```bash
git add vts/mcp/server.py tests/mcp/test_server_integration.py vts/__init__.py
git commit -m "feat(mcp): expose cursor pagination on list_tasks tool; bump version (vts-kdc)"
```

---

## Self-Review

**Spec coverage:**
- Repo status filter → Task 1. ✓
- Opaque cursor codec → Task 2. ✓
- TaskPage schema + tool rework (drop sort/order, add cursor) → Task 3. ✓
- FastMCP wrapper + docstring + integration test → Task 4. ✓
- Tests: repo, codec, tool unit, integration → Tasks 1-4. ✓
- Existing tests updated to new contract (test_tools_list, test_server_integration, conftest FakeRepo) → Tasks 3, 4. ✓
- Web endpoint unaffected (status defaults None) → asserted in Task 4 Step 4. ✓
- Version bump → Task 4 Step 5. ✓

**Placeholder scan:** none — all code is concrete. The one judgment call (integration-test structured-content access) explicitly says "match the existing test's access pattern" and to read it first, not a placeholder.

**Type consistency:** `list_tasks_page(..., status=...)` signature identical across repo (Task 1), protocol + tool (Task 3), FakeRepo (Task 3). `TaskPage(tasks, next_cursor, has_more)` consistent across schema (Task 3), tool return (Task 3), wrapper (Task 4), tests. `encode_cursor`/`decode_cursor` names consistent (Task 2 → Task 3). The `status` enum-vs-str bridge is handled in both the real path (`TaskStatus(status)`) and the fake (`getattr(status,"value",status)`).
