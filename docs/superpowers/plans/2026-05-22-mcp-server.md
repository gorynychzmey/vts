# MCP Server for vts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose vts as an MCP server (in-process, mounted on the existing webapi) so MCP clients can submit video URLs, list tasks, and retrieve raw/redacted transcripts and summaries — with a Redis-backed `wait_for_task`.

**Architecture:** New package `vts/mcp/` builds a `FastMCP` instance and exposes `build_mcp_app()`. `vts/api/main.py` mounts it at `/mcp` when `settings.mcp_enabled`. Auth reuses `vts.services.auth.require_user` against the HTTP request that FastMCP exposes via its `Context`. Tools call repository/service functions directly (no in-process HTTP). `wait_for_task` subscribes to the existing `{redis_prefix}events` pubsub channel **before** reading task state from the DB, so no event between subscribe-and-read is lost.

**Tech Stack:** Python 3.12, FastAPI 0.116, FastMCP 3.x, SQLAlchemy 2 (async), Redis (`redis.asyncio`), pytest, the existing `_FakeRedis` test pattern in `tests/test_redis_bus_cancel.py`.

**Spec:** [docs/superpowers/specs/2026-05-22-mcp-server-design.md](../specs/2026-05-22-mcp-server-design.md)

**Beads:** vts-163

---

## Notes about the codebase (read this once)

- `TaskStatus` is a `StrEnum` with members: `queued, running, paused, completed, archived, failed, canceled`. The spec used "done" informally — in the implementation use `completed`. Terminal statuses for `wait_for_task`: `completed | failed | canceled`.
- The event channel is `{settings.redis_prefix}events`. Payload (JSON): `{"user_id", "task_id", "event", "data"}`. The `task_status` event publishes `data.status` as the enum value string.
- The "transcript ready" signal is the `phase` event with `data == {"phase": "merge_transcript", "status": "done"}` (see [vts/pipeline/processor.py:1005-1006](../../../vts/pipeline/processor.py#L1005-L1006)). There is no dedicated "summary ready" phase event — use `task.summary_path` as the truth, re-checked on each wake-up.
- `AuthenticatedUser` resolution lives in [vts/services/auth.py](../../../vts/services/auth.py). It is a FastAPI `Depends`. We reuse the **body** of `require_user` from an MCP tool by passing the Starlette `Request` we get from the FastMCP `Context`. See Task 4.
- Existing tests use `from types import SimpleNamespace` and hand-rolled fakes (e.g. `_FakeRedis` in `tests/test_redis_bus_cancel.py`). Follow that style — do not introduce new test infra like `fakeredis` or async HTTP clients unless a task explicitly says to.
- Repo uses `bd` (beads) for tasks, not TodoWrite. Version bumps in `vts/__init__.py` are NOT required for docs-only commits, but ARE required for any commit that ships runnable code. Bump patch (`x.y.Z+1`) on each shipping commit unless otherwise instructed.

---

## File Structure

```
vts/
  mcp/
    __init__.py        — public surface: build_mcp_app()
    server.py          — FastMCP instance construction, tool registration
    auth.py            — MCP-context wrapper around vts.services.auth.require_user
    tools.py           — six tool implementations (pure async functions)
    schemas.py         — Pydantic response models for tools
  core/
    config.py          — add mcp_enabled, mcp_path
  api/
    main.py            — mount mcp app when enabled
tests/
  mcp/
    __init__.py
    conftest.py        — shared fakes (FakeRedis with pubsub, in-memory Task, etc.)
    test_tools_submit.py
    test_tools_list.py
    test_tools_status.py
    test_tools_transcript.py
    test_tools_summary.py
    test_tools_wait.py
    test_server_mount.py
requirements.txt       — add fastmcp
.env.example           — VTS_MCP_ENABLED, VTS_MCP_PATH
README.md              — short "MCP" section
```

Each tool implementation is a free async function in `vts/mcp/tools.py` that takes a `Context`-like dependency (resolved user + DB session + redis + settings). `vts/mcp/server.py` wires those functions into FastMCP's `@mcp.tool` registration. This split keeps tool logic testable without an MCP runtime.

---

## Task 1: Add FastMCP dependency and a smoke test

**Files:**
- Modify: `requirements.txt`
- Create: `tests/mcp/test_server_mount.py` (the repo uses namespace-style test discovery — do NOT add `tests/mcp/__init__.py`, otherwise a top-level `mcp` package will shadow the installed MCP SDK during test collection)

- [ ] **Step 1: Write the failing test** (`tests/mcp/test_server_mount.py`)

```python
from __future__ import annotations


def test_fastmcp_importable() -> None:
    """Smoke test: fastmcp is installed and exposes FastMCP."""
    from fastmcp import FastMCP

    mcp = FastMCP(name="vts-test")
    assert mcp.name == "vts-test"
```

- [ ] **Step 2: Run test, expect ImportError**

```bash
.venv/bin/python -m pytest tests/mcp/test_server_mount.py -v
```

Expected: `ModuleNotFoundError: No module named 'fastmcp'`.

- [ ] **Step 3: Add FastMCP to requirements**

Append to `requirements.txt`:

```
fastmcp>=3.3,<4
```

- [ ] **Step 4: Install and rerun the test**

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/mcp/test_server_mount.py -v
```

Expected: PASS.

- [ ] **Step 5: Bump version and commit**

Edit `vts/__init__.py`: bump patch (e.g. `0.5.22` → `0.5.23`).

```bash
git add requirements.txt tests/mcp/test_server_mount.py vts/__init__.py
git commit -m "chore(mcp): add fastmcp dependency"
```

---

## Task 2: Add `mcp_enabled` / `mcp_path` settings

**Files:**
- Modify: `vts/core/config.py` (add two fields next to other booleans, e.g. after line ~80)
- Modify: `.env.example`
- Create: `tests/mcp/test_settings.py`

- [ ] **Step 1: Write the failing test** (`tests/mcp/test_settings.py`)

```python
from __future__ import annotations

from vts.core.config import Settings


def test_mcp_defaults_enabled_at_root() -> None:
    s = Settings()
    assert s.mcp_enabled is True
    assert s.mcp_path == "/mcp"


def test_mcp_can_be_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_ENABLED", "false")
    monkeypatch.setenv("VTS_MCP_PATH", "/foo")
    s = Settings()
    assert s.mcp_enabled is False
    assert s.mcp_path == "/foo"
```

- [ ] **Step 2: Run, expect failure**

```bash
.venv/bin/python -m pytest tests/mcp/test_settings.py -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'mcp_enabled'`.

- [ ] **Step 3: Add settings**

In `vts/core/config.py`, add to class `Settings` (next to `timezone`):

```python
    mcp_enabled: bool = True
    mcp_path: str = "/mcp"
```

- [ ] **Step 4: Rerun**

Expected: PASS.

- [ ] **Step 5: Document in `.env.example`**

Append:

```
# MCP server
# Mount the MCP (Model Context Protocol) server inside the webapi.
# Default: enabled. Set to "false" to disable.
VTS_MCP_ENABLED=true
# Path the MCP server is mounted at (must start with /).
VTS_MCP_PATH=/mcp
```

- [ ] **Step 6: Bump version and commit**

Bump patch in `vts/__init__.py`.

```bash
git add vts/core/config.py .env.example tests/mcp/test_settings.py vts/__init__.py
git commit -m "feat(mcp): config flags VTS_MCP_ENABLED / VTS_MCP_PATH"
```

---

## Task 3: Build the empty FastMCP app and mount it on the webapi

We mount a do-nothing MCP app first so we have an end-to-end skeleton to grow from.

**Files:**
- Create: `vts/mcp/__init__.py`
- Create: `vts/mcp/server.py`
- Modify: `vts/api/main.py`
- Create: `tests/mcp/test_server_mount.py` (extend existing test file)

- [ ] **Step 1: Add a failing mount test**

Append to `tests/mcp/test_server_mount.py`:

```python
def test_build_mcp_app_returns_asgi_callable() -> None:
    from vts.mcp import build_mcp_app

    app = build_mcp_app()
    # ASGI app callable signature: scope, receive, send
    assert callable(app)


def test_webapi_mounts_mcp_when_enabled() -> None:
    """The FastAPI app should have a route mounted at the configured mcp_path."""
    import os

    os.environ["VTS_MCP_ENABLED"] = "true"
    os.environ["VTS_MCP_PATH"] = "/mcp"
    from vts.api.main import create_app

    app = create_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/mcp" in paths


def test_webapi_does_not_mount_mcp_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_ENABLED", "false")
    # Settings is cached — clear the lru_cache so the env change takes effect.
    from vts.core.config import get_settings
    get_settings.cache_clear()
    from vts.api.main import create_app

    app = create_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/mcp" not in paths
    get_settings.cache_clear()
```

- [ ] **Step 2: Run, expect failure**

Expected: `ModuleNotFoundError: No module named 'vts.mcp'`.

- [ ] **Step 3: Create `vts/mcp/__init__.py`**

```python
from __future__ import annotations

from vts.mcp.server import build_mcp_app

__all__ = ["build_mcp_app"]
```

- [ ] **Step 4: Create `vts/mcp/server.py`**

```python
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server. Tools are registered in later tasks."""
    mcp = FastMCP(name="vts")
    return mcp


def build_mcp_app() -> Any:
    """Return an ASGI app suitable for mounting in FastAPI.

    FastMCP exposes a Streamable HTTP transport via `http_app()` (FastMCP 3.x).
    The app is mountable as a sub-app on any ASGI host.
    """
    mcp = build_mcp_server()
    return mcp.http_app()
```

Note for the implementer: the exact ASGI accessor on FastMCP 3.x is `mcp.http_app()`. If a runtime/import error reveals a different name, grep the installed `fastmcp` package (`find .venv -path '*/fastmcp/*' -name '*.py' | xargs grep -l 'def http_app\|streamable_http_app'`) and use the correct accessor — do NOT add a shim or fallback.

- [ ] **Step 5: Mount in `vts/api/main.py`**

In [vts/api/main.py:375](../../../vts/api/main.py#L375) area (after `app.mount("/static", ...)`), add:

```python
    if settings.mcp_enabled:
        from vts.mcp import build_mcp_app
        app.mount(settings.mcp_path, build_mcp_app())
```

The `settings` variable is already in scope inside `create_app()` — confirm by reading the surrounding lines before editing.

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/mcp/ -v
```

Expected: all three tests in `test_server_mount.py` pass.

- [ ] **Step 7: Bump version and commit**

```bash
git add vts/mcp/ vts/api/main.py tests/mcp/test_server_mount.py vts/__init__.py
git commit -m "feat(mcp): mount FastMCP app on webapi (no tools yet)"
```

---

## Task 4: MCP-context auth adapter

We reuse `require_user` semantics by extracting the user from a Starlette `Request`. FastMCP exposes the underlying HTTP request via `Context`. We do **not** call `require_user` directly (it's a FastAPI `Depends`); instead we factor its body into a request-only helper that both REST and MCP call.

**Files:**
- Modify: `vts/services/auth.py` — extract a `resolve_user_from_request(request, session, settings)` helper; rewrite `require_user` to call it.
- Create: `vts/mcp/auth.py`
- Create: `tests/mcp/test_auth.py`

- [ ] **Step 1: Write the failing test** (`tests/mcp/test_auth.py`)

```python
from __future__ import annotations

import pytest
from starlette.requests import Request
from types import SimpleNamespace

from vts.core.config import Settings
from vts.services.auth import resolve_user_from_request


def _make_request(headers: dict[str, str], client_host: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": (client_host, 12345),
    }
    return Request(scope)


class _FakeRepo:
    def __init__(self) -> None:
        self.users: dict[str, SimpleNamespace] = {}

    async def get_or_create_user(self, username: str) -> SimpleNamespace:
        if username not in self.users:
            self.users[username] = SimpleNamespace(id=f"id-{username}", username=username)
        return self.users[username]


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_resolve_user_from_request_happy_path(monkeypatch) -> None:
    settings = Settings(trusted_proxy_cidrs=["127.0.0.1/32"])
    request = _make_request({"X-Forwarded-User": "alice"})
    session = _FakeSession()
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)

    user = await resolve_user_from_request(request, session, settings)
    assert user.username == "alice"
    assert session.committed is True


@pytest.mark.asyncio
async def test_resolve_user_from_request_rejects_untrusted_proxy() -> None:
    from fastapi import HTTPException

    settings = Settings(trusted_proxy_cidrs=["10.0.0.0/8"])
    request = _make_request({"X-Forwarded-User": "alice"}, client_host="8.8.8.8")
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await resolve_user_from_request(request, session, settings)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_user_from_request_missing_header() -> None:
    from fastapi import HTTPException

    settings = Settings(trusted_proxy_cidrs=["127.0.0.1/32"], environment="prod")
    request = _make_request({})
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await resolve_user_from_request(request, session, settings)
    assert excinfo.value.status_code == 401
```

The test needs `pytest-asyncio`. Check `requirements-dev.txt`. If missing, add it as part of this task:

```
pytest-asyncio>=0.24,<1
```

…and add `asyncio_mode = "auto"` to `pyproject.toml` `[tool.pytest.ini_options]` if a `pyproject.toml` exists, else add a `pytest.ini`. Check what's in the repo first (`ls pyproject.toml pytest.ini 2>/dev/null`) and follow the existing pattern. If neither file exists, create `pytest.ini`:

```
[pytest]
asyncio_mode = auto
```

- [ ] **Step 2: Run, expect failure**

```bash
.venv/bin/python -m pytest tests/mcp/test_auth.py -v
```

Expected: `ImportError: cannot import name 'resolve_user_from_request'`.

- [ ] **Step 3: Refactor `vts/services/auth.py`**

Replace the body of `require_user` with a call to a new `resolve_user_from_request`. Final shape:

```python
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from vts.core.config import Settings, get_settings
from vts.db.repo import Repo
from vts.db.session import get_db_session


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    username: str
    requested_by: str
    is_admin: bool
    acting_as: str


async def resolve_user_from_request(
    request: StarletteRequest,
    session: AsyncSession,
    settings: Settings,
) -> AuthenticatedUser:
    """Core auth logic, callable from both FastAPI Depends and FastMCP tools."""
    remote_host = request.client.host if request.client else "127.0.0.1"
    if not settings.is_trusted_proxy(remote_host):
        raise HTTPException(status_code=403, detail="Untrusted proxy source for forwarded auth header")
    x_forwarded_user = request.headers.get("x-forwarded-user")
    if not x_forwarded_user and settings.environment != "prod":
        x_forwarded_user = request.query_params.get("dev_user")
    if not x_forwarded_user:
        raise HTTPException(status_code=401, detail="Missing X-Forwarded-User header")

    requested_by = x_forwarded_user.strip()
    is_admin = settings.is_admin(requested_by)
    acting_as = requested_by
    requested_as = request.query_params.get("as_user")
    if requested_as:
        candidate = requested_as.strip()
        if not candidate:
            raise HTTPException(status_code=400, detail="Empty as_user value")
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can switch user context")
        acting_as = candidate

    repo = Repo(session)
    if requested_as:
        user = await repo.get_user_by_username(acting_as)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target user not found for admin switch",
            )
    else:
        user = await repo.get_or_create_user(acting_as)
    await session.commit()
    return AuthenticatedUser(
        id=str(user.id),
        username=user.username,
        requested_by=requested_by,
        is_admin=is_admin,
        acting_as=acting_as,
    )


async def require_user(
    request: Request,
    x_forwarded_user: str | None = Header(default=None, alias="X-Forwarded-User"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    # x_forwarded_user kept as a FastAPI Header param for OpenAPI docs only;
    # the resolver reads it directly from the request.
    _ = x_forwarded_user
    return await resolve_user_from_request(request, session, settings)
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
.venv/bin/python -m pytest tests/mcp/test_auth.py -v
```

- [ ] **Step 5: Make sure existing tests still pass**

```bash
.venv/bin/python -m pytest -q
```

Expected: green (we only refactored; behavior is unchanged).

- [ ] **Step 6: Write the MCP auth helper** (`vts/mcp/auth.py`)

```python
from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request

from vts.core.config import Settings, get_settings
from vts.db.session import get_db_session_factory
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(http_request: Request) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the user for an MCP tool invocation.

    Returns (user, settings). Raises HTTPException on auth failure (401/403);
    the FastMCP layer translates these into MCP errors.
    """
    settings = get_settings()
    session_factory = get_db_session_factory()
    async with session_factory() as session:
        user = await resolve_user_from_request(http_request, session, settings)
    return user, settings
```

Check that `vts/db/session.py` exposes `get_db_session_factory` (or similar). If the existing accessor is named differently (e.g. `get_sessionmaker`), use that name. Confirm with:

```bash
grep -n "^def \|^async def \|sessionmaker\|async_sessionmaker" vts/db/session.py
```

If only `get_db_session` (the dependency) exists, add a sibling `get_db_session_factory()` that returns the sessionmaker — no behavioral change to existing code.

- [ ] **Step 7: Bump version and commit**

```bash
git add vts/services/auth.py vts/mcp/auth.py vts/db/session.py tests/mcp/test_auth.py requirements-dev.txt pytest.ini pyproject.toml vts/__init__.py
git commit -m "refactor(auth): extract resolve_user_from_request; add MCP auth adapter"
```

(Stage only files you actually changed.)

---

## Task 5: Pydantic schemas for MCP tool responses

**Files:**
- Create: `vts/mcp/schemas.py`
- Create: `tests/mcp/test_schemas.py`

- [ ] **Step 1: Write the failing test** (`tests/mcp/test_schemas.py`)

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from vts.mcp.schemas import (
    SubmitVideoResult,
    TaskSummary,
    TaskStatusResult,
    TranscriptResult,
    SummaryResult,
    WaitResult,
)


def test_submit_video_result_shape() -> None:
    r = SubmitVideoResult(task_id=uuid.uuid4(), status="queued", created_at=datetime.now(tz=timezone.utc))
    d = r.model_dump(mode="json")
    assert set(d) == {"task_id", "status", "created_at"}


def test_task_summary_shape() -> None:
    r = TaskSummary(
        task_id=uuid.uuid4(),
        status="completed",
        title="hi",
        url="https://x",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )
    d = r.model_dump(mode="json")
    assert set(d) == {"task_id", "status", "title", "url", "created_at", "updated_at"}


def test_task_status_result_includes_progress() -> None:
    r = TaskStatusResult(
        task_id=uuid.uuid4(),
        status="running",
        stage="transcribing",
        asr_progress=0.5,
        summary_progress=0.0,
        error=None,
        updated_at=datetime.now(tz=timezone.utc),
    )
    d = r.model_dump(mode="json")
    assert d["asr_progress"] == 0.5


def test_transcript_and_summary_shapes() -> None:
    tr = TranscriptResult(task_id=uuid.uuid4(), variant="raw", content="abc", format="txt")
    su = SummaryResult(task_id=uuid.uuid4(), content="# md", format="markdown")
    assert tr.format in {"txt", "json"}
    assert su.format == "markdown"


def test_wait_result_reached_flag() -> None:
    r = WaitResult(
        task_id=uuid.uuid4(),
        status="completed",
        reached=True,
        stage="done",
        updated_at=datetime.now(tz=timezone.utc),
    )
    assert r.reached is True
```

- [ ] **Step 2: Run, expect failure**

Expected: `ModuleNotFoundError: No module named 'vts.mcp.schemas'`.

- [ ] **Step 3: Create `vts/mcp/schemas.py`**

```python
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


TaskStatusLiteral = Literal[
    "queued", "running", "paused", "completed", "archived", "failed", "canceled"
]


class SubmitVideoResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    created_at: datetime


class TaskSummary(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    title: str | None
    url: str | None
    created_at: datetime
    updated_at: datetime


class TaskStatusResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    stage: str | None
    asr_progress: float
    summary_progress: float
    error: str | None
    updated_at: datetime


class TranscriptResult(BaseModel):
    task_id: uuid.UUID
    variant: Literal["raw", "redacted"]
    content: str
    format: Literal["txt", "json"]


class SummaryResult(BaseModel):
    task_id: uuid.UUID
    content: str
    format: Literal["markdown"]


class WaitResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    reached: bool
    stage: str | None
    updated_at: datetime
```

- [ ] **Step 4: Run tests, expect PASS**

- [ ] **Step 5: Bump version and commit**

```bash
git add vts/mcp/schemas.py tests/mcp/test_schemas.py vts/__init__.py
git commit -m "feat(mcp): response schemas"
```

---

## Task 6: Tool — `submit_video`

This tool calls the same code path as `POST /api/tasks`. Read [vts/api/main.py:512-555](../../../vts/api/main.py#L512-L555) first to see what fields it sets and how it publishes `notify_queued()`. Mirror that, calling repository + bus directly — do NOT issue an in-process HTTP request.

**Files:**
- Modify: `vts/mcp/tools.py` (create)
- Create: `tests/mcp/conftest.py` (shared fakes)
- Create: `tests/mcp/test_tools_submit.py`

- [ ] **Step 1: Add shared fakes** (`tests/mcp/conftest.py`)

```python
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class FakeTask:
    id: uuid.UUID
    user_id: uuid.UUID
    source_url: str
    source_title: str | None = None
    status: str = "queued"
    artifact_dir: str = "/tmp/vts-test/task"
    transcript_path: str | None = None
    summary_path: str | None = None
    error_message: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    summary_progress: dict[str, int] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class FakeRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, FakeTask] = {}

    async def create_task(self, *, user_id, source_url, source_title, artifact_dir, options) -> FakeTask:
        task = FakeTask(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url=source_url,
            source_title=source_title,
            artifact_dir=artifact_dir,
            options=options or {},
        )
        self.tasks[task.id] = task
        return task

    async def get_task_for_user(self, user_id, task_id) -> FakeTask | None:
        t = self.tasks.get(task_id)
        if t is None or t.user_id != user_id:
            return None
        return t

    async def list_tasks_for_user(self, user_id, *, status=None, limit=20, sort="updated_at", order="desc"):
        items = [t for t in self.tasks.values() if t.user_id == user_id]
        if status:
            items = [t for t in items if t.status == status]
        key_map = {
            "created_at": lambda t: t.created_at,
            "updated_at": lambda t: t.updated_at,
            "title": lambda t: (t.source_title or ""),
        }
        items.sort(key=key_map[sort], reverse=(order == "desc"))
        return items[:limit]


class FakeBus:
    def __init__(self) -> None:
        self.queued_notifications = 0
        self.published: list[dict[str, Any]] = []
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    async def notify_queued(self) -> None:
        self.queued_notifications += 1

    async def publish_event(self, *, user_id, task_id, event, data, throttle_key=None) -> None:
        payload = {"user_id": user_id, "task_id": task_id, "event": event, "data": data}
        self.published.append(payload)
        for q in list(self._subscribers):
            q.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)


@dataclass
class FakeUser:
    id: str
    username: str = "alice"

    @property
    def id_uuid(self) -> uuid.UUID:
        return uuid.UUID(self.id) if isinstance(self.id, str) and "-" in self.id else uuid.uuid4()
```

- [ ] **Step 2: Write the failing test** (`tests/mcp/test_tools_submit.py`)

```python
from __future__ import annotations

import uuid

import pytest

from tests.mcp.conftest import FakeBus, FakeRepo, FakeUser
from vts.mcp.tools import submit_video


@pytest.mark.asyncio
async def test_submit_video_creates_task_and_notifies(tmp_path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    bus = FakeBus()

    result = await submit_video(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title=None,
        user=user,
        repo=repo,
        bus=bus,
        artifacts_root=tmp_path,
    )

    assert result.status == "queued"
    assert result.task_id in repo.tasks
    assert bus.queued_notifications == 1


@pytest.mark.asyncio
async def test_submit_video_rejects_blank_url(tmp_path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    bus = FakeBus()
    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="   ",
            title=None,
            user=user,
            repo=repo,
            bus=bus,
            artifacts_root=tmp_path,
        )
    assert exc.value.status_code == 422
```

- [ ] **Step 3: Run, expect failure**

Expected: `ModuleNotFoundError: No module named 'vts.mcp.tools'`.

- [ ] **Step 4: Implement `submit_video` in `vts/mcp/tools.py`**

```python
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException

from vts.mcp.schemas import SubmitVideoResult


async def submit_video(
    *,
    url: str,
    title: str | None,
    user,
    repo,
    bus,
    artifacts_root: Path,
) -> SubmitVideoResult:
    if not url or not url.strip():
        raise HTTPException(status_code=422, detail="url is required")
    user_uuid = uuid.UUID(user.id)
    artifact_dir = artifacts_root / str(user_uuid) / "tasks" / str(uuid.uuid4())
    task = await repo.create_task(
        user_id=user_uuid,
        source_url=url.strip(),
        source_title=(title or None),
        artifact_dir=str(artifact_dir),
        options={},
    )
    await bus.notify_queued()
    return SubmitVideoResult(task_id=task.id, status=task.status, created_at=task.created_at)
```

Note: the REST endpoint in `vts/api/main.py` does more (e.g. de-dup, artifact dir mkdir, additional options). When implementing, re-read [vts/api/main.py:512-555](../../../vts/api/main.py#L512-L555) and mirror the exact production semantics — do **not** invent a simpler variant. The pseudo-implementation above shows the shape; the real one delegates to whatever helper REST uses (likely `repo.create_task(...)` directly).

- [ ] **Step 5: Run tests, expect PASS**

- [ ] **Step 6: Bump version and commit**

```bash
git add vts/mcp/tools.py tests/mcp/conftest.py tests/mcp/test_tools_submit.py vts/__init__.py
git commit -m "feat(mcp): submit_video tool"
```

---

## Task 7: Tool — `list_tasks`

**Files:**
- Modify: `vts/mcp/tools.py`
- Create: `tests/mcp/test_tools_list.py`

- [ ] **Step 1: Failing test** (`tests/mcp/test_tools_list.py`)

```python
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import list_tasks


def _seed(repo: FakeRepo, user_id: uuid.UUID, n: int = 3) -> list[FakeTask]:
    base = datetime.now(tz=timezone.utc)
    tasks = []
    for i in range(n):
        t = FakeTask(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url=f"https://x/{i}",
            source_title=f"title-{i}",
            status="completed" if i % 2 == 0 else "running",
            created_at=base - timedelta(minutes=10 - i),
            updated_at=base - timedelta(minutes=5 - i),
        )
        repo.tasks[t.id] = t
        tasks.append(t)
    return tasks


@pytest.mark.asyncio
async def test_list_tasks_default_sort_is_updated_at_desc() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 3)

    out = await list_tasks(user=user, repo=repo, status=None, limit=20, sort="updated_at", order="desc")
    times = [r.updated_at for r in out]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_list_tasks_status_filter() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 4)

    out = await list_tasks(user=user, repo=repo, status="completed", limit=20, sort="updated_at", order="desc")
    assert all(r.status == "completed" for r in out)


@pytest.mark.asyncio
async def test_list_tasks_caps_limit_at_100() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, status=None, limit=999, sort="updated_at", order="desc")
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement `list_tasks` in `vts/mcp/tools.py`**

```python
import uuid as _uuid
from typing import Literal
from vts.mcp.schemas import TaskSummary


async def list_tasks(
    *,
    user,
    repo,
    status: Literal["queued", "running", "completed", "failed", "paused"] | None = None,
    limit: int = 20,
    sort: Literal["created_at", "updated_at", "title"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
) -> list[TaskSummary]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    items = await repo.list_tasks_for_user(
        _uuid.UUID(user.id),
        status=status,
        limit=limit,
        sort=sort,
        order=order,
    )
    return [
        TaskSummary(
            task_id=t.id,
            status=t.status,
            title=t.source_title,
            url=t.source_url,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in items
    ]
```

Note: the existing `Repo` in [vts/db/repo.py](../../../vts/db/repo.py) does not currently expose `list_tasks_for_user(...)` with `sort`/`order` parameters — read it to confirm. If the existing method does not accept these, **extend it in this task** with a new method `list_tasks_for_user_sorted(...)` (or add the keyword args, default-equivalent to existing behavior). Tests in `tests/test_*` that exercise the existing list endpoint must keep passing. Run the full suite after.

- [ ] **Step 4: Run tests, expect PASS**

- [ ] **Step 5: Bump version and commit**

```bash
git add vts/mcp/tools.py vts/db/repo.py tests/mcp/test_tools_list.py vts/__init__.py
git commit -m "feat(mcp): list_tasks tool with status/sort/limit"
```

---

## Task 8: Tool — `get_status`

**Files:**
- Modify: `vts/mcp/tools.py`
- Create: `tests/mcp/test_tools_status.py`

- [ ] **Step 1: Failing test**

```python
from __future__ import annotations

import uuid

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_status


@pytest.mark.asyncio
async def test_get_status_returns_snapshot() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="running")
    repo.tasks[t.id] = t

    result = await get_status(task_id=t.id, user=user, repo=repo)
    assert result.task_id == t.id
    assert result.status == "running"


@pytest.mark.asyncio
async def test_get_status_404_when_not_owned() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    other = FakeTask(id=uuid.uuid4(), user_id=uuid.uuid4(), source_url="x")
    repo.tasks[other.id] = other

    with pytest.raises(HTTPException) as exc:
        await get_status(task_id=other.id, user=user, repo=repo)
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Implement**

```python
from vts.mcp.schemas import TaskStatusResult


async def get_status(*, task_id: _uuid.UUID, user, repo) -> TaskStatusResult:
    task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    asr_progress, summary_progress = _progress_for_task(task)  # see note below
    return TaskStatusResult(
        task_id=task.id,
        status=task.status,
        stage=_stage_label(task),
        asr_progress=asr_progress,
        summary_progress=summary_progress,
        error=task.error_message,
        updated_at=task.updated_at,
    )
```

For `_progress_for_task` and `_stage_label`: the REST `serialize_task` helper in [vts/api/main.py](../../../vts/api/main.py) already computes both. Either (a) extract those helpers into `vts/services/` so they're callable from MCP, or (b) call them directly from `vts/api/main` (acceptable — no circular dep yet) and use their return shape. The first is cleaner; do (a) if it requires moving < 50 lines. Otherwise (b) and leave a TODO-free note in the commit message that future cleanup should move them.

- [ ] **Step 3: Run, PASS**
- [ ] **Step 4: Bump, commit**

```bash
git commit -m "feat(mcp): get_status tool"
```

---

## Task 9: Tool — `get_transcript`

**Files:**
- Modify: `vts/mcp/tools.py`
- Create: `tests/mcp/test_tools_transcript.py`

- [ ] **Step 1: Failing test**

```python
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_transcript


@pytest.mark.asyncio
async def test_get_transcript_raw_txt(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("hello world", encoding="utf-8")
    t = FakeTask(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user.id),
        source_url="x",
        transcript_path=str(transcript),
        artifact_dir=str(tmp_path),
    )
    repo.tasks[t.id] = t

    res = await get_transcript(task_id=t.id, variant="raw", user=user, repo=repo)
    assert res.content == "hello world"
    assert res.format == "txt"


@pytest.mark.asyncio
async def test_get_transcript_redacted(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "redacted_transcript.txt").write_text("redacted ok", encoding="utf-8")
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", artifact_dir=str(tmp_path))
    repo.tasks[t.id] = t

    res = await get_transcript(task_id=t.id, variant="redacted", user=user, repo=repo)
    assert res.content == "redacted ok"
    assert res.format == "txt"


@pytest.mark.asyncio
async def test_get_transcript_raw_not_ready(tmp_path: Path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", artifact_dir=str(tmp_path))
    repo.tasks[t.id] = t

    with pytest.raises(HTTPException) as exc:
        await get_transcript(task_id=t.id, variant="raw", user=user, repo=repo)
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Implement** in `vts/mcp/tools.py`

```python
from pathlib import Path as _Path
from typing import Literal as _Literal

from vts.mcp.schemas import TranscriptResult


async def get_transcript(
    *,
    task_id: _uuid.UUID,
    variant: _Literal["raw", "redacted"],
    user,
    repo,
) -> TranscriptResult:
    task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if variant == "raw":
        if not task.transcript_path:
            raise HTTPException(status_code=404, detail="Transcript is not ready")
        path = _Path(task.transcript_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Transcript file missing")
        fmt = "txt" if path.suffix == ".txt" else "json"
    else:  # redacted
        path = _Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
        fmt = "txt"
    return TranscriptResult(
        task_id=task.id,
        variant=variant,
        content=path.read_text(encoding="utf-8"),
        format=fmt,
    )
```

- [ ] **Step 3: Run, PASS**
- [ ] **Step 4: Bump, commit**

```bash
git commit -m "feat(mcp): get_transcript tool (raw|redacted)"
```

---

## Task 10: Tool — `get_summary`

**Files:**
- Modify: `vts/mcp/tools.py`
- Create: `tests/mcp/test_tools_summary.py`

- [ ] **Step 1: Failing test**

```python
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_summary


@pytest.mark.asyncio
async def test_get_summary_markdown(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    summary = tmp_path / "summary.md"
    summary.write_text("# Summary\nbody", encoding="utf-8")
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", summary_path=str(summary))
    repo.tasks[t.id] = t

    res = await get_summary(task_id=t.id, user=user, repo=repo)
    assert res.content.startswith("# Summary")
    assert res.format == "markdown"


@pytest.mark.asyncio
async def test_get_summary_not_ready() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x")
    repo.tasks[t.id] = t
    with pytest.raises(HTTPException) as exc:
        await get_summary(task_id=t.id, user=user, repo=repo)
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Implement**

```python
from vts.mcp.schemas import SummaryResult


async def get_summary(*, task_id: _uuid.UUID, user, repo) -> SummaryResult:
    task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.summary_path:
        raise HTTPException(status_code=404, detail="Summary is not ready")
    path = _Path(task.summary_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Summary file missing")
    return SummaryResult(task_id=task.id, content=path.read_text(encoding="utf-8"), format="markdown")
```

- [ ] **Step 3: Run, PASS**
- [ ] **Step 4: Commit**

```bash
git commit -m "feat(mcp): get_summary tool"
```

---

## Task 11: Tool — `wait_for_task` (Redis-backed)

This is the trickiest tool. It uses the **subscribe-then-check** pattern against the existing `{redis_prefix}events` pubsub channel. Read [vts/api/main.py:903-928](../../../vts/api/main.py#L903-L928) for the SSE handler — we copy its subscription discipline.

**Files:**
- Modify: `vts/mcp/tools.py`
- Create: `tests/mcp/test_tools_wait.py`

The tests use a `FakePubSub` that supports `subscribe`/`unsubscribe`/`get_message`. Add it to `tests/mcp/conftest.py`.

- [ ] **Step 1: Extend `tests/mcp/conftest.py`** — add `FakeRedisWithPubSub`

```python
import json


class _FakePubSub:
    def __init__(self, redis: "FakeRedisWithPubSub") -> None:
        self._redis = redis
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._redis._subscribers.setdefault(channel, []).append(self)

    async def unsubscribe(self, channel: str | None = None) -> None:
        chans = list(self._channels) if channel is None else [channel]
        for ch in chans:
            subs = self._redis._subscribers.get(ch, [])
            if self in subs:
                subs.remove(self)
            self._channels.discard(ch)

    async def close(self) -> None:
        await self.unsubscribe()

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None):
        try:
            payload = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "data": json.dumps(payload).encode("utf-8")}


class FakeRedisWithPubSub:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[_FakePubSub]] = {}

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        for sub in self._subscribers.get(channel, []):
            sub._queue.put_nowait(payload)
```

- [ ] **Step 2: Failing test** (`tests/mcp/test_tools_wait.py`)

```python
from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask, FakeRedisWithPubSub
from vts.mcp.tools import wait_for_task


@pytest.mark.asyncio
async def test_wait_returns_immediately_if_terminal() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="completed")
    repo.tasks[t.id] = t

    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=5,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
    assert res.status == "completed"


@pytest.mark.asyncio
async def test_wait_unblocks_on_task_status_event() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="running")
    repo.tasks[t.id] = t

    async def publish_later():
        await asyncio.sleep(0.05)
        t.status = "completed"
        await redis.publish("vts:events", {
            "user_id": user.id,
            "task_id": str(t.id),
            "event": "task_status",
            "data": {"status": "completed"},
        })

    asyncio.create_task(publish_later())
    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=2,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
    assert res.status == "completed"


@pytest.mark.asyncio
async def test_wait_timeout_returns_reached_false() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="running")
    repo.tasks[t.id] = t

    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=1,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is False
    assert res.status == "running"


@pytest.mark.asyncio
async def test_wait_filters_other_users_events() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    other = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="running")
    repo.tasks[t.id] = t

    async def publish_noise():
        await asyncio.sleep(0.05)
        await redis.publish("vts:events", {
            "user_id": other.id,
            "task_id": str(t.id),
            "event": "task_status",
            "data": {"status": "completed"},
        })

    asyncio.create_task(publish_noise())
    res = await wait_for_task(
        task_id=t.id,
        until="done",
        timeout_seconds=1,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is False


@pytest.mark.asyncio
async def test_wait_for_transcript_until(tmp_path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    redis = FakeRedisWithPubSub()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", status="running")
    repo.tasks[t.id] = t

    async def publish_phase():
        await asyncio.sleep(0.05)
        # Mirror what the pipeline emits at vts/pipeline/processor.py:1005
        await redis.publish("vts:events", {
            "user_id": user.id,
            "task_id": str(t.id),
            "event": "phase",
            "data": {"phase": "merge_transcript", "status": "done"},
        })

    asyncio.create_task(publish_phase())
    res = await wait_for_task(
        task_id=t.id,
        until="transcript",
        timeout_seconds=2,
        user=user,
        repo=repo,
        redis=redis,
        events_channel="vts:events",
    )
    assert res.reached is True
```

- [ ] **Step 3: Run, expect failure**

- [ ] **Step 4: Implement `wait_for_task`** in `vts/mcp/tools.py`

```python
import asyncio as _asyncio
import json as _json

from vts.mcp.schemas import WaitResult


_TERMINAL = {"completed", "failed", "canceled"}


def _wait_condition_met(task, until: str) -> bool:
    if task.status in _TERMINAL:
        return True
    if until == "transcript":
        return bool(task.transcript_path)
    if until == "summary":
        return bool(task.summary_path)
    return False  # until == "done" already handled by terminal check


def _event_implies_target(event_name: str, data: dict, until: str) -> bool:
    if event_name == "task_status" and data.get("status") in _TERMINAL:
        return True
    if until == "transcript" and event_name == "phase" \
            and data.get("phase") == "merge_transcript" and data.get("status") == "done":
        return True
    # For until == "summary" there is no dedicated phase event; we rely on
    # the DB re-check on each wake-up (handled by the loop).
    return False


async def wait_for_task(
    *,
    task_id,
    until: str = "done",
    timeout_seconds: int = 300,
    user,
    repo,
    redis,
    events_channel: str,
) -> WaitResult:
    if until not in {"transcript", "summary", "done"}:
        raise HTTPException(status_code=422, detail="invalid 'until' value")
    if timeout_seconds < 1 or timeout_seconds > 1800:
        raise HTTPException(status_code=422, detail="timeout_seconds must be 1..1800")

    pubsub = redis.pubsub()
    await pubsub.subscribe(events_channel)
    try:
        # subscribe-then-check: any event after `subscribe` is buffered.
        task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if _wait_condition_met(task, until):
            return WaitResult(
                task_id=task.id, status=task.status, reached=True,
                stage=None, updated_at=task.updated_at,
            )

        deadline = _asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - _asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=min(remaining, 5.0))
            if not msg:
                # periodic re-check covers the no-phase-for-summary case
                task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
                if task and _wait_condition_met(task, until):
                    return WaitResult(
                        task_id=task.id, status=task.status, reached=True,
                        stage=None, updated_at=task.updated_at,
                    )
                continue
            payload = _json.loads(msg["data"].decode("utf-8"))
            if payload.get("user_id") != user.id:
                continue
            if payload.get("task_id") != str(task_id):
                continue
            if _event_implies_target(payload.get("event", ""), payload.get("data") or {}, until):
                task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
                return WaitResult(
                    task_id=task.id, status=task.status, reached=True,
                    stage=None, updated_at=task.updated_at,
                )

        task = await repo.get_task_for_user(_uuid.UUID(user.id), task_id)
        return WaitResult(
            task_id=task.id, status=task.status, reached=False,
            stage=None, updated_at=task.updated_at,
        )
    finally:
        await pubsub.unsubscribe(events_channel)
        await pubsub.close()
```

- [ ] **Step 5: Run all wait tests, expect PASS**

```bash
.venv/bin/python -m pytest tests/mcp/test_tools_wait.py -v
```

- [ ] **Step 6: Bump version and commit**

```bash
git commit -m "feat(mcp): wait_for_task with Redis subscribe-then-check"
```

---

## Task 12: Wire tools into the FastMCP server

Now we expose the six free functions as FastMCP tools, doing per-call auth + dependency resolution (DB session, repo, bus, redis, settings).

**Files:**
- Modify: `vts/mcp/server.py`
- Create: `tests/mcp/test_server_tools_registered.py`

- [ ] **Step 1: Failing test**

```python
from __future__ import annotations


def test_server_registers_expected_tools() -> None:
    from vts.mcp.server import build_mcp_server

    mcp = build_mcp_server()
    names = {tool.name for tool in mcp.tools}  # FastMCP 3.x exposes .tools
    expected = {"submit_video", "list_tasks", "get_status", "get_transcript", "get_summary", "wait_for_task"}
    assert expected.issubset(names)
```

If `mcp.tools` is not the correct accessor in FastMCP 3.x, grep the installed package to find the right one (`grep -rn "def tools\|@property" .venv/lib/*/site-packages/fastmcp/ | head -30`) and update both the test and assertion.

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Register tools** in `vts/mcp/server.py`

```python
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastmcp import Context, FastMCP
from redis.asyncio import Redis

from vts.core.config import get_settings
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import (
    SubmitVideoResult,
    SummaryResult,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.mcp.tools import (
    get_status,
    get_summary,
    get_transcript,
    list_tasks,
    submit_video,
    wait_for_task,
)
from vts.services.redis_bus import RedisBus


def _get_http_request(ctx: Context):
    """Pull the underlying Starlette request out of the FastMCP context.

    FastMCP 3.x exposes it via `ctx.request_context.request`. Verify the
    exact attribute name on first run; if different, fix here (and only here).
    """
    return ctx.request_context.request


def build_mcp_server() -> FastMCP:
    mcp = FastMCP(name="vts")

    @mcp.tool
    async def submit_video_tool(ctx: Context, url: str, title: str | None = None) -> SubmitVideoResult:
        """Submit a video URL for processing. Returns task_id immediately."""
        http_req = _get_http_request(ctx)
        user, settings = await mcp_authenticate(http_req)
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            repo = Repo(session)
            redis = Redis.from_url(settings.redis_url, decode_responses=False)
            try:
                bus = RedisBus(redis, settings)
                result = await submit_video(
                    url=url, title=title, user=user, repo=repo, bus=bus,
                    artifacts_root=settings.artifacts_root,
                )
                await session.commit()
                return result
            finally:
                await redis.aclose()

    @mcp.tool
    async def list_tasks_tool(
        ctx: Context,
        status: Literal["queued", "running", "completed", "failed", "paused"] | None = None,
        limit: int = 20,
        sort: Literal["created_at", "updated_at", "title"] = "updated_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> list[TaskSummary]:
        """List tasks owned by the calling user."""
        user, _settings = await mcp_authenticate(_get_http_request(ctx))
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            return await list_tasks(
                user=user, repo=Repo(session),
                status=status, limit=limit, sort=sort, order=order,
            )

    @mcp.tool
    async def get_status_tool(ctx: Context, task_id: uuid.UUID) -> TaskStatusResult:
        """Get current pipeline status for one task."""
        user, _settings = await mcp_authenticate(_get_http_request(ctx))
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            return await get_status(task_id=task_id, user=user, repo=Repo(session))

    @mcp.tool
    async def get_transcript_tool(
        ctx: Context, task_id: uuid.UUID, variant: Literal["raw", "redacted"] = "raw"
    ) -> TranscriptResult:
        """Fetch the transcript text. variant=raw is the ASR output, variant=redacted is the processed version."""
        user, _settings = await mcp_authenticate(_get_http_request(ctx))
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            return await get_transcript(task_id=task_id, variant=variant, user=user, repo=Repo(session))

    @mcp.tool
    async def get_summary_tool(ctx: Context, task_id: uuid.UUID) -> SummaryResult:
        """Fetch the markdown summary for a task."""
        user, _settings = await mcp_authenticate(_get_http_request(ctx))
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            return await get_summary(task_id=task_id, user=user, repo=Repo(session))

    @mcp.tool
    async def wait_for_task_tool(
        ctx: Context,
        task_id: uuid.UUID,
        until: Literal["transcript", "summary", "done"] = "done",
        timeout_seconds: int = 300,
    ) -> WaitResult:
        """Block until the task reaches the target stage or the timeout fires."""
        user, settings = await mcp_authenticate(_get_http_request(ctx))
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        session_factory = get_db_session_factory()
        try:
            async with session_factory() as session:
                return await wait_for_task(
                    task_id=task_id, until=until, timeout_seconds=timeout_seconds,
                    user=user, repo=Repo(session), redis=redis,
                    events_channel=f"{settings.redis_prefix}events",
                )
        finally:
            await redis.aclose()

    return mcp


def build_mcp_app() -> Any:
    return build_mcp_server().http_app()
```

The tool names exposed to MCP clients are `submit_video`, `list_tasks`, `get_status`, `get_transcript`, `get_summary`, `wait_for_task` (FastMCP uses the function name by default; the `_tool` suffix above is for the inner Python symbol — if FastMCP picks it up verbatim and gives clients `submit_video_tool`, switch to `@mcp.tool(name="submit_video")` syntax). Confirm by running the test in Step 1 — it asserts on the public names.

- [ ] **Step 4: Run all MCP tests**

```bash
.venv/bin/python -m pytest tests/mcp/ -v
```

Expected: all green.

- [ ] **Step 5: Full suite to confirm no regressions**

```bash
.venv/bin/python -m pytest -q
```

- [ ] **Step 6: Bump version and commit**

```bash
git commit -m "feat(mcp): register six tools on FastMCP server"
```

---

## Task 13: README + .env.example documentation

**Files:**
- Modify: `README.md` (add a section)
- Modify: `.env.example` (already done in Task 2 — verify)

- [ ] **Step 1: Append README section**

Add after the "Quick start" section, before "Stack":

````markdown
## MCP server

vts exposes a Model Context Protocol (MCP) server in the same process as the
webapi, mounted at `/mcp` by default. MCP clients (Claude Desktop, Claude
Code, etc.) can submit videos and pull back transcripts and summaries.

**Tools exposed:**

- `submit_video(url, title?)` — submit a URL for processing; returns a
  `task_id` immediately.
- `list_tasks(status?, limit?, sort?, order?)` — list your tasks.
- `get_status(task_id)` — poll status and progress.
- `get_transcript(task_id, variant="raw"|"redacted")` — fetch the raw ASR
  transcript or the processed (redacted) one.
- `get_summary(task_id)` — fetch the markdown summary.
- `wait_for_task(task_id, until="done"|"transcript"|"summary", timeout_seconds?)`
  — block until the task reaches the target stage.

**Auth:** identical to the REST API — the MCP endpoint sits behind the same
reverse proxy and reads `X-Forwarded-User`.

**Example Claude Desktop config:**

```json
{
  "mcpServers": {
    "vts": {
      "type": "http",
      "url": "https://vts.example.com/mcp"
    }
  }
}
```

Disable the MCP server with `VTS_MCP_ENABLED=false` or change the mount
path with `VTS_MCP_PATH=/some/other/path`.
````

- [ ] **Step 2: Bump version and commit**

```bash
git add README.md vts/__init__.py
git commit -m "docs(mcp): README section and config knobs"
```

---

## Task 14: Final regression sweep + push

- [ ] **Step 1: Run full test suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: green.

- [ ] **Step 2: Lint / type-check if the project has one**

```bash
ls .pre-commit-config.yaml ruff.toml mypy.ini 2>/dev/null
```

Run whatever is configured. If nothing is configured, skip.

- [ ] **Step 3: Close beads issue, push**

```bash
bd close vts-163
git pull --rebase
bd dolt push
git push
git status   # must say "up to date with origin"
```

---

## Self-Review (against spec)

Spec coverage walkthrough (✓ = covered, see task):

- Architecture / mount at `/mcp` — Tasks 2, 3.
- New code layout `vts/mcp/{server,auth,tools,schemas}.py` — Tasks 3, 4, 5, 6–12.
- Transport: Streamable HTTP via `FastMCP.http_app()` — Task 3.
- Tool: `submit_video` — Task 6.
- Tool: `list_tasks` with status/sort/order/limit + cap at 100 — Task 7.
- Tool: `get_status` — Task 8.
- Tool: `get_transcript` (raw|redacted) — Task 9.
- Tool: `get_summary` — Task 10.
- Tool: `wait_for_task` Redis-backed, subscribe-then-check — Task 11.
- Auth via `X-Forwarded-User`, shared with REST — Task 4.
- Config: `mcp_enabled`, `mcp_path` — Task 2.
- Dependencies: `fastmcp>=3.3,<4` — Task 1.
- Tests: per-tool unit + tool-registration; matches existing fake-based style — Tasks 6–12.
- Docs: README + .env.example — Tasks 2, 13.
- Explicit out-of-scope (pause/resume/upload/paging/API keys/`status_changed_at`) — none of these appear as tasks. ✓
- "Open implementation questions" from spec (phase event name; FastMCP version) — resolved in the "Notes about the codebase" section at the top of this plan. ✓

Type-consistency check: tool names (`submit_video`, `list_tasks`, `get_status`, `get_transcript`, `get_summary`, `wait_for_task`) match across Tasks 6–12, the registration in Task 12, and the registration test. Schema names (`SubmitVideoResult`, `TaskSummary`, `TaskStatusResult`, `TranscriptResult`, `SummaryResult`, `WaitResult`) are consistent everywhere they appear.

Placeholder scan: no "TBD"/"TODO"/"implement later"/"similar to Task N" in the plan body. Where an implementer judgment is required at coding time (verifying the FastMCP 3.x accessor name, deciding whether to extract `serialize_task` helpers vs. import them), the plan names the exact verification step and acceptable outcomes — not a placeholder.
