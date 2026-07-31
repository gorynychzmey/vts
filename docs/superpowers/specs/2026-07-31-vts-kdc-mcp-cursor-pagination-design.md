# vts-kdc — Cursor pagination for the MCP `list_tasks` tool

**Issue:** vts-kdc (VOS-84 follow-up, "VOS-84c")
**Date:** 2026-07-31
**Status:** Design approved, ready for plan

## Scope

Let MCP clients page through **all** of their tasks. Today the MCP `list_tasks`
tool ([vts/mcp/tools.py:203](../../../vts/mcp/tools.py), wrapper at
[vts/mcp/server.py:137](../../../vts/mcp/server.py)) is `limit`-only (max 100) —
there is no way to reach tasks beyond the first page. This adds opaque-cursor
pagination, reusing the VOS-84 `Repo.list_tasks_page` composite `(created_at, id)`
cursor.

**In scope:**
- Opaque cursor pagination on the MCP `list_tasks` tool (created_at DESC).
- Optional `status` filter preserved on the paginated path.
- A `TaskPage` result shape carrying `next_cursor` + `has_more`.

**Out of scope (separate issues):**
- Filter by name / date / type — VOS-84b (vts-rhx). When it lands, its
  repo-level filter can be threaded into `list_tasks_page` and exposed here too.
- Any change to the web `/api/tasks` endpoint (already done in VOS-84).

## Decisions (agreed)

1. **Cursor keyed on `created_at` + optional `status` filter** — not full
   cursor support for `updated_at`/`title` sort (an `updated_at` cursor is
   unstable: a task's `updated_at` changes mid-pagination, so the cursor could
   skip or repeat rows). `created_at` is immutable → a stable cursor.
2. **Opaque cursor string** — the tool returns `next_cursor`; the client passes
   it back verbatim. Far better MCP/LLM ergonomics than juggling `ts`+`id`.
3. **Drop `sort`/`order` from the MCP tool (breaking its schema)** — the tool
   becomes always newest-first (`created_at DESC`) paginated. One obvious way to
   list is cleaner for an LLM than a `sort` param that only paginates for one of
   its values. MCP is versioned by the tool schema; no known consumer relies on
   `sort=title`/`updated_at`.

## Section 1 — Repo: add `status` filter to `list_tasks_page`

Extend the existing method (do not change its cursor/order semantics):

```python
async def list_tasks_page(
    self,
    user_id: uuid.UUID,
    *,
    before: tuple[datetime, uuid.UUID] | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
    order: str = "desc",
    limit: int,
    status: TaskStatus | None = None,   # NEW
) -> list[Task]:
```

Add one clause after the user filter: `if status is not None: stmt = stmt.where(Task.status == status)`.
The composite `(created_at, id)` cursor stays correct — the status predicate
only narrows the set; ordering and the tuple comparison are unchanged. Existing
web-API callers pass no `status` → behavior identical. `import` the `TaskStatus`
enum (already imported in repo.py).

## Section 2 — Cursor codec

New module `vts/mcp/cursor.py` (small, single responsibility, easy to unit-test):

```python
def encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str
def decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]   # raises ValueError on bad input
```

- Encoding: `base64url( f"{created_at.isoformat()}|{task_id}" )` (no padding).
- Decoding: reverse; on any malformation (bad base64, missing `|`, unparseable
  datetime/uuid) raise `ValueError`. The tool layer converts that to
  `HTTPException(status_code=422, detail="invalid cursor")` — consistent with the
  existing MCP tools' 422 style (`list_tasks` already raises 422 on bad `limit`).
- Opaque by construction: clients never build it; they echo `next_cursor`.

## Section 3 — MCP tool `list_tasks`

### New result schema (`vts/mcp/schemas.py`)

```python
class TaskPage(BaseModel):
    tasks: list[TaskSummary]
    next_cursor: str | None   # None when this is the last page
    has_more: bool
```

`TaskSummary` is unchanged.

### Tool function (`vts/mcp/tools.py::list_tasks`)

New signature (drops `sort`/`order`, adds `cursor`):

```python
async def list_tasks(
    *,
    user: _UserLike,
    repo: _RepoListLike,
    status: TaskStatusLiteral | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> TaskPage:
```

Logic:
- Validate `limit` in `1..100` (unchanged 422).
- `before = decode_cursor(cursor)` if `cursor` else `None`; a `ValueError` →
  `HTTPException(422, "invalid cursor")`.
- Map the `status` literal to the `TaskStatus` enum (or `None`).
- `tasks = await repo.list_tasks_page(user_id, before=before, order="desc", limit=limit, status=status_enum)`.
- `has_more = len(tasks) == limit` (a full page implies there may be more).
- `next_cursor = encode_cursor(tasks[-1].created_at, tasks[-1].id) if has_more and tasks else None`.
- Build `TaskSummary` rows exactly as today; return `TaskPage(tasks=..., next_cursor=..., has_more=...)`.

Note: `has_more` is a heuristic (a full final page yields `has_more=True` and a
`next_cursor` that returns an empty page). This is the standard, acceptable
cursor-pagination trade-off; the alternative (fetch `limit+1`) is not worth the
extra row here. The client stops when `tasks` comes back empty or `has_more` is
false.

The `_RepoListLike` protocol in tools.py gains the `list_tasks_page` signature
(it currently declares `list_tasks_for_user_filtered`; add or replace so the
type shim matches what the tool now calls).

### FastMCP wrapper (`vts/mcp/server.py:137`)

```python
@mcp.tool(name="list_tasks")
async def _list_tasks(
    status: Literal["queued","running","paused","completed","archived","failed","canceled"] | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> TaskPage:
    """List the calling user's tasks, newest first, in pages.

    Returns up to `limit` tasks plus `next_cursor`. To get the next page,
    call again with `cursor` set to the `next_cursor` from the previous
    response. When `has_more` is false (or `next_cursor` is null), there are
    no more tasks. Optionally filter by `status`.
    """
    ...
    return await list_tasks(user=user, repo=Repo(session), status=status, limit=limit, cursor=cursor)
```

The docstring matters — it is what the LLM reads to drive pagination.

## Section 4 — Tests

**Repo (`tests/test_list_tasks_page_repo.py`, extend):**
- `status` filter returns only matching tasks; cursor still pages correctly
  within the filtered set (seed mixed statuses, page with a status filter, assert
  no cross-status leakage and no skipped/duplicated rows across pages).

**Cursor codec (`tests/test_mcp_cursor.py`, new):**
- round-trip `encode`→`decode` preserves `(created_at, id)` exactly (incl. a
  fixed-width-microsecond datetime).
- `decode` raises `ValueError` on: non-base64, missing `|`, bad datetime, bad
  uuid.

**MCP tool (`tests/mcp/` — follow the existing MCP tool-test pattern):**
- First call (no cursor) returns ≤`limit` tasks newest-first, `has_more`/`next_cursor`
  correct when more exist.
- Second call with the returned `next_cursor` returns the following page with no
  overlap and correct order.
- Last page: `has_more=False`, `next_cursor=None`.
- `status` filter narrows results.
- Invalid `cursor` → 422.
- `limit` out of range → 422 (unchanged).

## Files touched

- `vts/db/repo.py` — `list_tasks_page` gains `status`.
- `vts/mcp/cursor.py` — new codec.
- `vts/mcp/schemas.py` — new `TaskPage`.
- `vts/mcp/tools.py` — `list_tasks` reworked to paginated `TaskPage`; protocol shim.
- `vts/mcp/server.py` — tool wrapper signature + docstring.
- Tests as in Section 4.
- `vts/__init__.py` — version bump before final commit.

## Compatibility notes

- Web `/api/tasks` untouched — `list_tasks_page`'s new `status` param defaults to
  `None`, so the endpoint (which never passes it) is unchanged.
- The MCP tool's schema changes (drops `sort`/`order`, return type becomes
  `TaskPage`). Intentional, agreed breaking change.

### Existing tests that MUST be updated to the new contract (verified present)

- `tests/mcp/test_tools_list.py` — calls `list_tasks(..., sort=..., order=...)`
  and asserts a `list`. Rewrite to the `cursor` signature and `TaskPage` return;
  add the new pagination/status/invalid-cursor cases here.
- `tests/mcp/test_server_integration.py:62` — `client.call_tool("list_tasks", {})`
  asserts `list[TaskSummary]`. Update to expect the `TaskPage` structured shape.
- `tests/mcp/conftest.py:178` — the fake repo declares
  `list_tasks_for_user_filtered`; add a `list_tasks_page` method to the fake so
  the tool's new call path is exercised.
- `tests/mcp/test_server_tools_registered.py` — only checks the tool *name* is
  registered; should still pass (name unchanged) — confirm, don't rewrite.
- `tests/test_openapi_spec.py::test_list_tasks_exposes_pagination_and_compact`
  asserts the WEB endpoint, not MCP — should be unaffected; confirm it still
  passes.

Update assertions to the new shape — never weaken them.
