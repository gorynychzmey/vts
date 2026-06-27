# Custom Prompts (VOS-63) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users create custom prompts and select, per task, a set of prompts (built-in summary + custom) applied independently to the prepared transcript, each producing its own saved result.

**Architecture:** A new `prompts` table holds per-user custom prompts; built-in prompts stay as files described by an in-code registry. Task selection is stored as a list of `PromptRef {source,id}` in `task.options.prompts`. The pipeline's heavy map-reduce runs once; the final stage loops over selected prompts, emitting one DAG step and one result file per prompt. HTTP and MCP drop the boolean `summary` for the new `prompts` list (breaking change).

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy (async) + Alembic, FastMCP, Redis bus, vanilla JS frontend, pytest.

**Spec:** [docs/superpowers/specs/2026-06-27-custom-prompts-design.md](../specs/2026-06-27-custom-prompts-design.md)

## Global Constraints

- Version bump: set `__version__` in `vts/__init__.py` before committing (current `1.0.95`).
- `PromptRef` shape is fixed everywhere: `{"source": "system" | "user", "id": str}`. For system, `id` is the registry key (e.g. `"summary"`). For user, `id` is the prompt UUID as string.
- Built-in summary keeps writing `summary/final.md` and `task.summary_path` / `task.summary_progress` unchanged — old tasks must keep working with no data migration.
- Old `options.summary` interpretation when `options.prompts` is absent: `summary` truthy or missing → `[{system,summary}]`; `summary is False` → `[]`.
- Per-prompt finalize DAG step name: `f"finalize:{source}:{id}"`. Built-in summary step remains literally `"summarize_final"` (source=system, id=summary) for back-compat with restart logic and existing weights.
- i18n: any new user-facing string gets keys in `vts/static/i18n/en.json`, `ru.json`, `de.json`.
- Migrations are Alembic; new revision chains from `0009_api_tokens`.

---

## File Structure

**New files:**
- `vts/services/prompt_registry.py` — system-prompt registry (`SYSTEM_PROMPTS`, `list_system_prompts`, `resolve_prompt_ref`), and `PromptRef` parsing/normalisation helpers.
- `alembic/versions/0010_prompts.py` — `prompts` table migration.
- `tests/test_prompt_registry.py`, `tests/test_prompts_repo.py`, `tests/test_prompts_api.py`, `tests/test_prompt_selection.py`, `tests/test_mcp_prompts.py`.

**Modified files:**
- `vts/db/models.py` — add `Prompt` model.
- `vts/db/repo.py` — CRUD for prompts.
- `vts/api/schemas.py` — `PromptRef`, `PromptOut`, `PromptCreateRequest`, `PromptUpdateRequest`; change `TaskCreateRequest`.
- `vts/api/main.py` — prompt CRUD endpoints, result endpoint, change `create_task`.
- `vts/services/task_progress.py` — selection normalisation helper (shared).
- `vts/pipeline/types.py` — split DAG into static head + dynamic finalize tail builder.
- `vts/pipeline/processor.py` — loop final stage over selected prompts; per-prompt result files + `prompt_results` index.
- `vts/mcp/tools.py` + `vts/mcp/server.py` + `vts/mcp/schemas.py` — drop `summary`, add `prompts`; add prompt CRUD + `get_prompt_result`; remove `get_summary`.
- `vts/static/index.html`, `vts/static/app.js`, `vts/static/styles.css`, `vts/static/i18n/*.json` — multiselect, manager panel, results dropdown, progress.
- `CHANGELOG.md` (new) — record breaking change.

---

## Phase 1 — System-prompt registry + ref helpers

### Task 1: `PromptRef` parsing and system-prompt registry

**Files:**
- Create: `vts/services/prompt_registry.py`
- Test: `tests/test_prompt_registry.py`

**Interfaces:**
- Produces:
  - `SYSTEM_PROMPTS: list[SystemPromptDef]` where `SystemPromptDef` is a frozen dataclass `(key: str, file: str, i18n_name_key: str)`. Initial content: one entry `SystemPromptDef("summary", "global_prompt.md", "prompt.system.summary")`.
  - `list_system_prompts() -> list[SystemPromptDef]`
  - `system_prompt_keys() -> set[str]`
  - `parse_ref(value: dict | str) -> tuple[str, str]` → returns `(source, id)`; accepts a dict `{"source","id"}` or a `"source:id"` string; raises `ValueError` on bad source or empty id.
  - `ref_to_dict(source: str, id: str) -> dict` → `{"source": source, "id": id}`
  - `ref_key(source: str, id: str) -> str` → `f"{source}:{id}"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompt_registry.py
import pytest
from vts.services.prompt_registry import (
    SYSTEM_PROMPTS, list_system_prompts, system_prompt_keys,
    parse_ref, ref_to_dict, ref_key,
)


def test_summary_is_registered():
    keys = system_prompt_keys()
    assert "summary" in keys
    summary = next(p for p in list_system_prompts() if p.key == "summary")
    assert summary.file == "global_prompt.md"
    assert summary.i18n_name_key == "prompt.system.summary"


def test_parse_ref_from_dict():
    assert parse_ref({"source": "user", "id": "abc"}) == ("user", "abc")


def test_parse_ref_from_string():
    assert parse_ref("system:summary") == ("system", "summary")


def test_parse_ref_rejects_bad_source():
    with pytest.raises(ValueError):
        parse_ref({"source": "nope", "id": "x"})


def test_parse_ref_rejects_empty_id():
    with pytest.raises(ValueError):
        parse_ref({"source": "user", "id": ""})


def test_ref_helpers_roundtrip():
    assert ref_to_dict("system", "summary") == {"source": "system", "id": "summary"}
    assert ref_key("user", "abc") == "user:abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompt_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vts.services.prompt_registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# vts/services/prompt_registry.py
from __future__ import annotations

from dataclasses import dataclass

VALID_SOURCES = {"system", "user"}


@dataclass(frozen=True)
class SystemPromptDef:
    key: str
    file: str
    i18n_name_key: str


SYSTEM_PROMPTS: list[SystemPromptDef] = [
    SystemPromptDef("summary", "global_prompt.md", "prompt.system.summary"),
]


def list_system_prompts() -> list[SystemPromptDef]:
    return list(SYSTEM_PROMPTS)


def system_prompt_keys() -> set[str]:
    return {p.key for p in SYSTEM_PROMPTS}


def parse_ref(value: dict | str) -> tuple[str, str]:
    if isinstance(value, str):
        source, _, ref_id = value.partition(":")
    elif isinstance(value, dict):
        source = str(value.get("source", ""))
        ref_id = str(value.get("id", ""))
    else:
        raise ValueError(f"invalid prompt ref: {value!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid prompt source: {source!r}")
    if not ref_id:
        raise ValueError("prompt ref id must not be empty")
    return source, ref_id


def ref_to_dict(source: str, id: str) -> dict:
    return {"source": source, "id": id}


def ref_key(source: str, id: str) -> str:
    return f"{source}:{id}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompt_registry.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/services/prompt_registry.py tests/test_prompt_registry.py
git commit -m "feat(prompts): system-prompt registry and PromptRef helpers (VOS-63)"
```

---

### Task 2: Selection normalisation (back-compat for old `summary` bool)

**Files:**
- Modify: `vts/services/task_progress.py`
- Test: `tests/test_prompt_selection.py`

**Interfaces:**
- Consumes: `parse_ref`, `ref_to_dict` from Task 1.
- Produces (in `task_progress.py`):
  - `selected_prompt_refs(options: dict) -> list[dict]` — returns normalised list of `{"source","id"}`. Rules: if `options["prompts"]` is a list, normalise each via `parse_ref`/`ref_to_dict` (dropping malformed entries); else fall back to legacy `summary`: `False` → `[]`, otherwise → `[{"system","summary"}]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompt_selection.py
from vts.services.task_progress import selected_prompt_refs


def test_explicit_prompts_list_normalised():
    opts = {"prompts": [{"source": "system", "id": "summary"},
                        {"source": "user", "id": "abc"}]}
    assert selected_prompt_refs(opts) == [
        {"source": "system", "id": "summary"},
        {"source": "user", "id": "abc"},
    ]


def test_empty_prompts_list_stays_empty():
    assert selected_prompt_refs({"prompts": []}) == []


def test_legacy_summary_true_maps_to_summary():
    assert selected_prompt_refs({"summary": True}) == [
        {"source": "system", "id": "summary"}]


def test_legacy_summary_missing_maps_to_summary():
    assert selected_prompt_refs({}) == [{"source": "system", "id": "summary"}]


def test_legacy_summary_false_maps_to_empty():
    assert selected_prompt_refs({"summary": False}) == []


def test_malformed_entries_dropped():
    opts = {"prompts": [{"source": "bad", "id": "x"},
                        {"source": "user", "id": "ok"}]}
    assert selected_prompt_refs(opts) == [{"source": "user", "id": "ok"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompt_selection.py -v`
Expected: FAIL with `ImportError: cannot import name 'selected_prompt_refs'`

- [ ] **Step 3: Write minimal implementation**

Append to `vts/services/task_progress.py`:

```python
from vts.services.prompt_registry import parse_ref, ref_to_dict


def selected_prompt_refs(options: dict) -> list[dict]:
    if isinstance(options, dict) and isinstance(options.get("prompts"), list):
        refs: list[dict] = []
        for entry in options["prompts"]:
            try:
                source, ref_id = parse_ref(entry)
            except (ValueError, TypeError):
                continue
            refs.append(ref_to_dict(source, ref_id))
        return refs
    summary = options.get("summary", True) if isinstance(options, dict) else True
    if summary is False:
        return []
    return [ref_to_dict("system", "summary")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompt_selection.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/services/task_progress.py tests/test_prompt_selection.py
git commit -m "feat(prompts): selected_prompt_refs with legacy summary fallback (VOS-63)"
```

---

## Phase 2 — Database: `prompts` table + repo CRUD

### Task 3: `Prompt` model + migration

**Files:**
- Modify: `vts/db/models.py` (after `ApiToken`, before `AsrSegment`)
- Create: `alembic/versions/0010_prompts.py`
- Test: `tests/test_prompts_repo.py` (model-import smoke test only in this task)

**Interfaces:**
- Produces: `vts.db.models.Prompt` with columns `id, user_id, name, system_prompt, created_at, updated_at`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_repo.py
from vts.db.models import Prompt


def test_prompt_model_columns():
    cols = set(Prompt.__table__.columns.keys())
    assert {"id", "user_id", "name", "system_prompt",
            "created_at", "updated_at"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts_repo.py -v`
Expected: FAIL with `ImportError: cannot import name 'Prompt'`

- [ ] **Step 3: Write minimal implementation**

Add to `vts/db/models.py` (uses already-imported `String`, `Text`, `DateTime`, `ForeignKey`, `Index`, `UUID`, `utcnow`):

```python
class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_prompts_user_created", "user_id", "created_at"),
    )
```

Create `alembic/versions/0010_prompts.py`:

```python
"""Add prompts table for user-defined custom prompts (VOS-63)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_prompts"
down_revision = "0009_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_prompts_user_created", "prompts", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_prompts_user_created", table_name="prompts")
    op.drop_table("prompts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompts_repo.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/db/models.py alembic/versions/0010_prompts.py tests/test_prompts_repo.py
git commit -m "feat(prompts): Prompt model and 0010 migration (VOS-63)"
```

---

### Task 4: Repo CRUD for prompts

**Files:**
- Modify: `vts/db/repo.py` (add methods after `touch_api_token_last_used`)
- Test: `tests/test_prompts_repo.py` (extend)

**Interfaces:**
- Consumes: `Prompt` model.
- Produces (on `Repo`):
  - `async create_prompt(user_id: uuid.UUID, name: str, system_prompt: str) -> Prompt`
  - `async list_prompts(user_id: uuid.UUID) -> list[Prompt]` — newest first
  - `async get_prompt(user_id: uuid.UUID, prompt_id: uuid.UUID) -> Prompt | None`
  - `async update_prompt(user_id, prompt_id, *, name: str | None, system_prompt: str | None) -> Prompt | None`
  - `async delete_prompt(user_id, prompt_id) -> bool`
  - `async set_task_prompt_results(task: Task, prompt_results: list[dict]) -> None` — JSON write-back helper for the `prompt_results` index (used by the pipeline in Task 9; defined here next to the other repo methods).

- [ ] **Step 1: Write the failing test**

Use the existing async DB fixture pattern. Check `tests/conftest.py` for the session fixture name (e.g. `session` / `db_session`) and reuse it; the test below assumes a `session` fixture yielding an `AsyncSession` and a helper to create a user. If the repo tests elsewhere create a user inline, mirror that.

```python
# tests/test_prompts_repo.py  (append)
import uuid
import pytest
from vts.db.repo import Repo
from vts.db.models import User


async def _make_user(session) -> uuid.UUID:
    user = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(user)
    await session.flush()
    return user.id


@pytest.mark.asyncio
async def test_prompt_crud_roundtrip(session):
    repo = Repo(session)
    uid = await _make_user(session)

    created = await repo.create_prompt(uid, "My Prompt", "Do the thing.")
    assert created.name == "My Prompt"

    listed = await repo.list_prompts(uid)
    assert [p.id for p in listed] == [created.id]

    fetched = await repo.get_prompt(uid, created.id)
    assert fetched is not None and fetched.system_prompt == "Do the thing."

    updated = await repo.update_prompt(uid, created.id, name="Renamed", system_prompt=None)
    assert updated is not None and updated.name == "Renamed"
    assert updated.system_prompt == "Do the thing."

    assert await repo.delete_prompt(uid, created.id) is True
    assert await repo.get_prompt(uid, created.id) is None


@pytest.mark.asyncio
async def test_prompt_isolation_between_users(session):
    repo = Repo(session)
    uid_a = await _make_user(session)
    uid_b = await _make_user(session)
    p = await repo.create_prompt(uid_a, "A", "a")
    assert await repo.get_prompt(uid_b, p.id) is None
    assert await repo.delete_prompt(uid_b, p.id) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts_repo.py -v`
Expected: FAIL with `AttributeError: 'Repo' object has no attribute 'create_prompt'`

- [ ] **Step 3: Write minimal implementation**

Add to `vts/db/repo.py` (imports `Prompt` from `vts.db.models`; `select`, `update`, `utcnow` are already imported in this module — verify and add `Prompt` to the models import line):

```python
    async def create_prompt(self, user_id: uuid.UUID, name: str, system_prompt: str) -> Prompt:
        prompt = Prompt(user_id=user_id, name=name, system_prompt=system_prompt)
        self.session.add(prompt)
        await self.session.flush()
        return prompt

    async def list_prompts(self, user_id: uuid.UUID) -> list[Prompt]:
        stmt = (
            select(Prompt)
            .where(Prompt.user_id == user_id)
            .order_by(Prompt.created_at.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> Prompt | None:
        stmt = select(Prompt).where(Prompt.id == prompt_id, Prompt.user_id == user_id)
        return await self.session.scalar(stmt)

    async def update_prompt(
        self,
        user_id: uuid.UUID,
        prompt_id: uuid.UUID,
        *,
        name: str | None,
        system_prompt: str | None,
    ) -> Prompt | None:
        prompt = await self.get_prompt(user_id, prompt_id)
        if prompt is None:
            return None
        if name is not None:
            prompt.name = name
        if system_prompt is not None:
            prompt.system_prompt = system_prompt
        await self.session.flush()
        return prompt

    async def delete_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> bool:
        prompt = await self.get_prompt(user_id, prompt_id)
        if prompt is None:
            return False
        await self.session.delete(prompt)
        await self.session.flush()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompts_repo.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_prompts_repo.py
git commit -m "feat(prompts): repo CRUD for user prompts (VOS-63)"
```

---

## Phase 3 — HTTP API: prompt CRUD, list, task selection, result endpoint

### Task 5: Pydantic schemas + `TaskCreateRequest` change

**Files:**
- Modify: `vts/api/schemas.py`
- Test: `tests/test_prompts_api.py` (schema-level tests)

**Interfaces:**
- Produces:
  - `PromptRef(BaseModel)`: `source: Literal["system","user"]`, `id: str = Field(min_length=1)`
  - `PromptOut(BaseModel)`: `source: str`, `id: str`, `name: str`, `editable: bool`
  - `PromptCreateRequest`: `name: str = Field(min_length=1, max_length=255)`, `system_prompt: str = Field(min_length=1)`
  - `PromptUpdateRequest`: `name: str | None = None`, `system_prompt: str | None = None`
  - `TaskCreateRequest`: drop `summary`; add `prompts: list[PromptRef] = Field(default_factory=lambda: [PromptRef(source="system", id="summary")])`. Validator: non-empty `prompts` requires `transcript`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_api.py
import pytest
from pydantic import ValidationError
from vts.api.schemas import (
    PromptRef, PromptCreateRequest, TaskCreateRequest,
)


def test_task_create_defaults_to_summary():
    req = TaskCreateRequest(url="https://x/y")
    assert req.prompts == [PromptRef(source="system", id="summary")]


def test_task_create_empty_prompts_allowed_without_summary():
    req = TaskCreateRequest(url="https://x/y", prompts=[])
    assert req.prompts == []


def test_non_empty_prompts_requires_transcript():
    with pytest.raises(ValidationError):
        TaskCreateRequest(url="https://x/y", transcript=False,
                          prompts=[PromptRef(source="system", id="summary")])


def test_prompt_create_request_validates():
    with pytest.raises(ValidationError):
        PromptCreateRequest(name="", system_prompt="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts_api.py -v`
Expected: FAIL with `ImportError: cannot import name 'PromptRef'`

- [ ] **Step 3: Write minimal implementation**

In `vts/api/schemas.py`, replace the `TaskCreateRequest` body and add the new models. The new `TaskCreateRequest`:

```python
class PromptRef(BaseModel):
    source: Literal["system", "user"]
    id: str = Field(min_length=1)


class PromptOut(BaseModel):
    source: str
    id: str
    name: str
    editable: bool


class PromptCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    system_prompt: str = Field(min_length=1)


class PromptUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    system_prompt: str | None = None


def _default_prompts() -> list["PromptRef"]:
    return [PromptRef(source="system", id="summary")]


class TaskCreateRequest(BaseModel):
    url: str = Field(min_length=3)
    language: str | None = None
    audio_only: bool = False
    transcript: bool = Field(default=True, validation_alias=AliasChoices("transcript", "do_transcribe"))
    prompts: list[PromptRef] = Field(default_factory=_default_prompts)

    @model_validator(mode="after")
    def validate_stage_dependencies(self) -> "TaskCreateRequest":
        if self.prompts and not self.transcript:
            raise ValueError("prompts require transcript")
        return self
```

Note: `Literal` is already imported in `schemas.py`. Remove the now-unused `summary` field and its old validator branch.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompts_api.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/api/schemas.py tests/test_prompts_api.py
git commit -m "feat(prompts): API schemas + TaskCreateRequest.prompts (VOS-63 breaking)"
```

---

### Task 6: Prompt CRUD + list endpoints

**Files:**
- Modify: `vts/api/main.py` (add after the api-tokens endpoints, ~L1080)
- Test: `tests/test_prompts_api.py` (extend with HTTP-client tests)

**Interfaces:**
- Consumes: `Repo.create_prompt/list_prompts/get_prompt/update_prompt/delete_prompt`, `list_system_prompts`, `PromptOut/PromptCreateRequest/PromptUpdateRequest`.
- Produces HTTP:
  - `GET /api/prompts` → `list[PromptOut]` (system first, then user newest-first). System `name` resolved via i18n for the request locale (Task 13 supplies keys; until then fall back to the i18n key string — acceptable, UI overrides display). `editable=False` for system, `True` for user.
  - `POST /api/prompts` → `PromptOut` (user)
  - `PATCH /api/prompts/{prompt_id}` → `PromptOut`
  - `DELETE /api/prompts/{prompt_id}` → 204

Use the existing test client / auth fixtures (mirror `tests/` HTTP tests — find an existing API test, e.g. one hitting `/api/me/tokens`, and copy its client+auth setup).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_api.py  (append; reuse the project's authed-client fixture, here named `client`)
import pytest


@pytest.mark.asyncio
async def test_prompts_list_includes_system_summary(client):
    resp = await client.get("/api/prompts")
    assert resp.status_code == 200
    body = resp.json()
    assert any(p["source"] == "system" and p["id"] == "summary" for p in body)
    summary = next(p for p in body if p["id"] == "summary")
    assert summary["editable"] is False


@pytest.mark.asyncio
async def test_prompt_create_list_update_delete(client):
    created = (await client.post("/api/prompts",
               json={"name": "Mine", "system_prompt": "Do X"})).json()
    assert created["source"] == "user" and created["editable"] is True
    pid = created["id"]

    listed = (await client.get("/api/prompts")).json()
    assert any(p["id"] == pid for p in listed)

    patched = (await client.patch(f"/api/prompts/{pid}",
               json={"name": "Renamed"})).json()
    assert patched["name"] == "Renamed"

    assert (await client.delete(f"/api/prompts/{pid}")).status_code == 204
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts_api.py -v -k "list_includes or create_list"`
Expected: FAIL with 404 (routes not registered)

- [ ] **Step 3: Write minimal implementation**

Add to `vts/api/main.py`:

```python
    @app.get("/api/prompts", response_model=list[PromptOut])
    async def list_prompts_endpoint(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> list[PromptOut]:
        from vts.services.prompt_registry import list_system_prompts
        out: list[PromptOut] = [
            PromptOut(source="system", id=p.key, name=p.i18n_name_key, editable=False)
            for p in list_system_prompts()
        ]
        repo = Repo(session)
        for row in await repo.list_prompts(uuid.UUID(user.id)):
            out.append(PromptOut(source="user", id=str(row.id), name=row.name, editable=True))
        return out

    @app.post("/api/prompts", response_model=PromptOut)
    async def create_prompt_endpoint(
        payload: PromptCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PromptOut:
        repo = Repo(session)
        row = await repo.create_prompt(uuid.UUID(user.id), payload.name.strip(), payload.system_prompt)
        await session.commit()
        return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)

    @app.patch("/api/prompts/{prompt_id}", response_model=PromptOut)
    async def update_prompt_endpoint(
        prompt_id: uuid.UUID,
        payload: PromptUpdateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PromptOut:
        repo = Repo(session)
        row = await repo.update_prompt(
            uuid.UUID(user.id), prompt_id,
            name=payload.name.strip() if payload.name is not None else None,
            system_prompt=payload.system_prompt,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        await session.commit()
        return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)

    @app.delete("/api/prompts/{prompt_id}", status_code=204)
    async def delete_prompt_endpoint(
        prompt_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        ok = await repo.delete_prompt(uuid.UUID(user.id), prompt_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Prompt not found")
        await session.commit()
        return Response(status_code=204)
```

Add `PromptOut, PromptCreateRequest, PromptUpdateRequest` to the schemas import in `main.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompts_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py tests/test_prompts_api.py
git commit -m "feat(prompts): HTTP CRUD + list endpoints (VOS-63)"
```

---

### Task 7: `create_task` persists `prompts`; result-read endpoint

**Files:**
- Modify: `vts/api/main.py` (`create_task` ~L1141; add result endpoint)
- Test: `tests/test_prompts_api.py` (extend)

**Interfaces:**
- Consumes: `TaskCreateRequest.prompts`, `selected_prompt_refs`.
- Produces:
  - `create_task` stores `options["prompts"] = [r.model_dump() for r in request.prompts]` and no longer stores `summary`.
  - `GET /api/tasks/{task_id}/results/{source}/{ref}` → plain text result for that prompt. Reads from `task.prompt_results` index (Phase 4 writes it); for `system/summary` falls back to `task.summary_path`. 404 if not found/owned.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_api.py  (append)
@pytest.mark.asyncio
async def test_create_task_stores_prompts_in_options(client, db_read):
    resp = await client.post("/api/tasks", json={
        "url": "https://example.com/v",
        "prompts": [{"source": "system", "id": "summary"}],
    })
    assert resp.status_code == 200
    task_id = resp.json()["id"]
    options = await db_read.task_options(task_id)  # helper: returns task.options dict
    assert options["prompts"] == [{"source": "system", "id": "summary"}]
    assert "summary" not in options
```

(If no `db_read` helper exists, assert via a follow-up `GET /api/tasks/{id}` whose `options` is exposed in `TaskOut`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompts_api.py -v -k stores_prompts`
Expected: FAIL — `options` still contains `summary`, no `prompts`.

- [ ] **Step 3: Write minimal implementation**

In `create_task` ([main.py:1154](../../../vts/api/main.py)), the line `options = request.model_dump()` already serialises `prompts` (list of dicts) since it's a Pydantic field, and drops `summary` (no longer a field). Confirm `options.pop("url", None)` stays. Then add the result endpoint:

```python
    @app.get("/api/tasks/{task_id}/results/{source}/{ref}", include_in_schema=False)
    async def get_prompt_result(
        task_id: uuid.UUID,
        source: str,
        ref: str,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PlainTextResponse:
        repo = Repo(session)
        task = await repo.get_task_by_id(task_id)
        if task is None or str(task.user_id) != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        from vts.services.prompt_results import resolve_result_path
        path = resolve_result_path(task, source, ref)
        if path is None or not Path(path).exists():
            raise HTTPException(status_code=404, detail="Result not found")
        return PlainTextResponse(Path(path).read_text(encoding="utf-8"))
```

Create `vts/services/prompt_results.py` with the shared index helpers (also used by the pipeline in Phase 4):

```python
# vts/services/prompt_results.py
from __future__ import annotations

from typing import Any

from vts.db.models import Task
from vts.services.prompt_registry import ref_key


def result_entries(task: Task) -> list[dict[str, Any]]:
    pr = task.options.get("prompt_results") if isinstance(task.options, dict) else None
    return pr if isinstance(pr, list) else []


def resolve_result_path(task: Task, source: str, ref: str) -> str | None:
    wanted = ref_key(source, ref)
    for entry in result_entries(task):
        if ref_key(str(entry.get("source")), str(entry.get("id"))) == wanted:
            path = entry.get("path")
            if isinstance(path, str) and path:
                return path
    if source == "system" and ref == "summary" and task.summary_path:
        return task.summary_path
    return None
```

> **Decision (spec §Результаты):** the `prompt_results` index lives inside `task.options` under key `prompt_results` (avoids a second migration). It is written by the pipeline (Phase 4).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompts_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py vts/services/prompt_results.py tests/test_prompts_api.py
git commit -m "feat(prompts): persist prompts in task options + result endpoint (VOS-63)"
```

---

## Phase 4 — Pipeline: per-prompt finalize loop + dynamic DAG tail

### Task 8: Dynamic DAG tail in `types.py`

**Files:**
- Modify: `vts/pipeline/types.py`
- Modify: `vts/pipeline/processor.py:243` (DAG iteration site)
- Test: `tests/test_dag_tail.py` (new)

**Interfaces:**
- Consumes: `selected_prompt_refs`, `ref_key`.
- Produces:
  - `DAG_HEAD: list[str]` = the static prefix up to and including `pack_window_notes` (everything except `summarize_final`).
  - `finalize_step_name(source: str, id: str) -> str` → `"summarize_final"` if `(system,summary)` else `f"finalize:{source}:{id}"`.
  - `build_dag_steps(options: dict) -> list[str]` → `DAG_HEAD + [finalize_step_name(...) for each selected ref]`. If no prompts selected, tail is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dag_tail.py
from vts.pipeline.types import DAG_HEAD, finalize_step_name, build_dag_steps


def test_summary_keeps_legacy_step_name():
    assert finalize_step_name("system", "summary") == "summarize_final"


def test_custom_prompt_step_name():
    assert finalize_step_name("user", "abc") == "finalize:user:abc"


def test_build_dag_summary_only():
    steps = build_dag_steps({"prompts": [{"source": "system", "id": "summary"}]})
    assert steps[-1] == "summarize_final"
    assert "pack_window_notes" in steps


def test_build_dag_summary_plus_custom():
    steps = build_dag_steps({"prompts": [
        {"source": "system", "id": "summary"},
        {"source": "user", "id": "abc"},
    ]})
    assert steps[-2:] == ["summarize_final", "finalize:user:abc"]


def test_build_dag_no_prompts_has_no_finalize():
    steps = build_dag_steps({"prompts": []})
    assert not any(s.startswith("finalize:") for s in steps)
    assert "summarize_final" not in steps
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dag_tail.py -v`
Expected: FAIL with `ImportError: cannot import name 'DAG_HEAD'`

- [ ] **Step 3: Write minimal implementation**

Rewrite `vts/pipeline/types.py`:

```python
from __future__ import annotations

from typing import Final

from vts.services.prompt_registry import ref_key
from vts.services.task_progress import selected_prompt_refs

DAG_HEAD: Final[list[str]] = [
    "download",
    "extract_audio",
    "trim_initial_silence",
    "segment_audio",
    "detect_language",
    "transcribe_segments",
    "merge_transcript",
    "prepare_llama_model",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
]

# Back-compat: the full static list (summary-only pipeline). Kept for any
# consumer that imported DAG_STEPS expecting the legacy shape.
DAG_STEPS: Final[list[str]] = DAG_HEAD + ["summarize_final"]


def finalize_step_name(source: str, id: str) -> str:
    if source == "system" and id == "summary":
        return "summarize_final"
    return f"finalize:{ref_key(source, id)}"


def build_dag_steps(options: dict) -> list[str]:
    tail = [finalize_step_name(r["source"], r["id"]) for r in selected_prompt_refs(options)]
    return DAG_HEAD + tail
```

In `processor.py:243`, change `for step_name in DAG_STEPS:` to build from the task's options:

```python
                for step_name in build_dag_steps(task_options):
```

(Add `build_dag_steps` to the `from vts.pipeline.types import ...` line in processor.py; locate `task_options` in that scope — it's the task's `options` dict already loaded near the DAG loop. If the variable is named differently there, use the loaded options dict.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dag_tail.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the existing pipeline tests to catch regressions**

Run: `pytest tests/test_pipeline_resume.py -v`
Expected: PASS (summary-only path unchanged because `build_dag_steps` of legacy options yields the old list).

- [ ] **Step 6: Commit**

```bash
git add vts/pipeline/types.py vts/pipeline/processor.py tests/test_dag_tail.py
git commit -m "feat(prompts): dynamic DAG finalize tail per selected prompt (VOS-63)"
```

---

### Task 9: Extract per-prompt finalize core + loop over prompts

**Files:**
- Modify: `vts/pipeline/processor.py` (`step_summarize_final` ~L1577 and the step-dispatch that maps step name → handler)
- Test: `tests/test_summarizer.py` (extend) and a focused `tests/test_finalize_loop.py`

**Interfaces:**
- Consumes: `selected_prompt_refs`, `finalize_step_name`, `Repo.set_task_prompt_results` (Task 4), `prompt_results` index helpers.
- Produces:
  - New `resolve_prompt_text(self, source, id, output_language, user_id) -> str` on the processor: for `system` loads `<file>.md` from `prompts_dir` (via registry) and renders language; for `user` loads `system_prompt` from DB via `Repo.get_prompt`. Raises if missing.
  - New `step_finalize_prompt(self, task_id, user_id, dirs, logger, task_options, dry_run, *, source, id)` — the generalised body of `step_summarize_final` parameterised by which prompt to run and which output files to write.
  - Result file path: `system/summary` → existing `summary/final.md` (+ keep `task.summary_path`); others → `summary/results/{source}__{id}.md`.
  - After each prompt completes, append/update its entry in `task.options["prompt_results"]` (via a repo helper `update_task_options` or in-place mutation + commit) with `{source,id,name,path,status:"completed"}`.

**Note on dispatch:** the step loop dispatches by step name. Add routing so that:
- `"summarize_final"` → `step_finalize_prompt(..., source="system", id="summary")`
- `"finalize:<source>:<id>"` → parse and call `step_finalize_prompt(..., source, id)`

- [ ] **Step 1: Write the failing test**

Drive at the unit boundary that doesn't need a live LLM. Test `resolve_prompt_text` and the result-index update; mock `self._llm.chat_completion` to return a fixed string (mirror how `tests/test_summarizer.py` stubs the LLM client).

```python
# tests/test_finalize_loop.py
import uuid
import pytest
from vts.services.prompt_results import result_entries


@pytest.mark.asyncio
async def test_finalize_writes_result_index_for_custom_prompt(processor_with_stub_llm, tmp_task_dirs, db_user_prompt):
    """After a custom-prompt finalize, options['prompt_results'] has its entry
    and the result file exists."""
    proc, task_id, user_id = processor_with_stub_llm
    pid = db_user_prompt  # a Prompt row id (str)
    options = {"prompts": [{"source": "user", "id": pid}]}
    await proc.step_finalize_prompt(
        task_id, user_id, tmp_task_dirs, proc.logger, options, dry_run=False,
        source="user", id=pid,
    )
    task = await proc._load_task(task_id)
    entries = result_entries(task)
    assert any(e["source"] == "user" and e["id"] == pid
               and e["status"] == "completed" for e in entries)
```

> The exact fixture names (`processor_with_stub_llm`, `tmp_task_dirs`, `db_user_prompt`) must match the harness in `tests/test_summarizer.py`. Before writing, read that file and reuse its existing processor/LLM-stub fixtures; adapt the test to the real fixture names. If no processor fixture exists, the minimum viable test stubs `_llm.chat_completion` and feeds a pre-written `summary/packed_notes.json` so the heavy path is skipped.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_finalize_loop.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'step_finalize_prompt'`

- [ ] **Step 3: Write minimal implementation**

Refactor `step_summarize_final` into `step_finalize_prompt(..., *, source, id)`:
1. Keep the existing "load packed notes / fall back to windows → `merged`" block verbatim (it is prompt-independent — this is the prepared input shared by all prompts).
2. Replace the hard-coded `global_prompt.md` load with:
   ```python
   final_prompt_base = await self.resolve_prompt_text(source, id, output_language, user_id)
   ```
   where for `system/summary` `resolve_prompt_text` returns exactly today's `global_prompt.md` rendering, so behaviour is identical.
3. Compute output paths:
   ```python
   if source == "system" and id == "summary":
       result_md = summary_dir / "final.md"
       result_json = summary_dir / "final.json"
   else:
       results_dir = summary_dir / "results"
       results_dir.mkdir(parents=True, exist_ok=True)
       result_md = results_dir / f"{source}__{id}.md"
       result_json = results_dir / f"{source}__{id}.json"
   ```
4. Keep the existing budget math + `chat_completion` call, writing to `result_md`/`result_json`.
5. For `system/summary`, keep setting `task.summary_path = str(result_md)` (back-compat). For all prompts, upsert the `prompt_results` entry.

Add `resolve_prompt_text`:

```python
    async def resolve_prompt_text(self, source: str, id: str, output_language: str, user_id: str) -> str:
        from vts.services.prompt_registry import list_system_prompts
        if source == "system":
            sysdef = next((p for p in list_system_prompts() if p.key == id), None)
            if sysdef is None:
                raise RuntimeError(f"unknown system prompt: {id}")
            base = self._render_prompt_with_language(
                load_prompt(self.settings.prompts_dir, sysdef.file,
                            "Produce a structured knowledge document from the notes.\n\nOutput language: ${LANG}."),
                output_language,
            )
            return base
        async with self.session_factory() as session:
            repo = Repo(session)
            row = await repo.get_prompt(uuid.UUID(user_id), uuid.UUID(id))
        if row is None:
            raise RuntimeError(f"user prompt not found: {id}")
        return self._render_prompt_with_language(row.system_prompt, output_language)
```

Add a `prompt_results` upsert helper (e.g. on the processor or in `prompt_results.py`):

```python
# vts/services/prompt_results.py  (append)
def upsert_result_entry(options: dict, source: str, id: str, name: str, path: str, status: str) -> None:
    entries = options.setdefault("prompt_results", [])
    target = ref_key(source, id)
    for e in entries:
        if ref_key(str(e.get("source")), str(e.get("id"))) == target:
            e.update(name=name, path=path, status=status)
            return
    entries.append({"source": source, "id": id, "name": name, "path": path, "status": status})
```

Wire dispatch where step handlers are selected by name (near `processor.py:243` loop / handler map). For a name starting with `finalize:`, parse `source,id` via `parse_ref(name.split(":", 1)[1])` and call `step_finalize_prompt`. For `summarize_final`, call with `source="system", id="summary"`.

**Important — JSON write-back (confirmed pattern):** `options` is a plain `JSON` column, so in-place dict mutation is NOT detected by SQLAlchemy. Follow the exact pattern used by `Repo.set_task_summary_progress` (it does `task.summary_progress = {...}; task.updated_at = utcnow(); await self.session.flush()`). Add a sibling repo method:

```python
    async def set_task_prompt_results(self, task: Task, prompt_results: list[dict]) -> None:
        new_options = dict(task.options or {})
        new_options["prompt_results"] = prompt_results
        task.options = new_options          # reassign so SQLAlchemy flushes it
        task.updated_at = utcnow()
        await self.session.flush()
```

Call it from `step_finalize_prompt` inside a `session_factory()` block + `await session.commit()`, mirroring `_persist_summary_progress` (`processor.py`). Use `upsert_result_entry` on a copy of the current options' `prompt_results` list, then pass the updated list to `set_task_prompt_results`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_finalize_loop.py tests/test_summarizer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/processor.py vts/services/prompt_results.py tests/test_finalize_loop.py
git commit -m "feat(prompts): per-prompt finalize loop + result index (VOS-63)"
```

---

## Phase 5 — MCP: drop `summary`/`get_summary`, add prompt tools

### Task 10: MCP `submit_video` uses `prompts`; add prompt CRUD tools; drop `get_summary`

**Files:**
- Modify: `vts/mcp/tools.py`, `vts/mcp/server.py`, `vts/mcp/schemas.py`
- Test: `tests/mcp/test_tools_prompts.py` (new) + update existing MCP tests that assert `summary`/`get_summary`:
  - `tests/mcp/test_tools_submit.py` — change `summary=False`/`summary=True` calls (L107, L133) to `prompts=[]` / `prompts=[{"source":"system","id":"summary"}]` and assert `options["prompts"]`.
  - `tests/mcp/test_tools_summary.py` — rename/rewrite for `get_prompt_result` (or delete and fold into the new file).
  - `tests/mcp/test_server_tools_registered.py` — update the registered-tool-name set (remove `get_summary`, add the five new tools).
- Reuse `FakeRepo` from `tests/mcp/conftest.py`; extend it to capture `last_options` if absent.

**Interfaces:**
- `submit_video(...)`: replace `summary: bool = True` param with `prompts: list[dict] | None = None`; when `None`, default to `[{"source":"system","id":"summary"}]`; validation: non-empty prompts requires `transcript`. Store `options["prompts"]` (normalised via `parse_ref`).
- New tool functions in `tools.py`: `list_prompts`, `create_prompt`, `update_prompt`, `delete_prompt`, `get_prompt_result(task_id, ref)`.
- `vts/mcp/server.py`: register `list_prompts`, `create_prompt`, `update_prompt`, `delete_prompt`, `get_prompt_result`; **remove** the `@mcp.tool(name="get_summary")` registration and the `get_summary` import.
- `vts/mcp/schemas.py`: remove `SummaryResult` if unused after; add `PromptInfo`, `PromptResult` result models as needed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_prompts.py
import pytest
from vts.mcp import tools


@pytest.mark.asyncio
async def test_submit_video_stores_prompts(fake_repo, fake_bus, tmp_path, fake_user):
    res = await tools.submit_video(
        url="https://x/y", user=fake_user, repo=fake_repo, bus=fake_bus,
        artifacts_root=tmp_path, prompts=None,
    )
    assert fake_repo.last_options["prompts"] == [{"source": "system", "id": "summary"}]
    assert "summary" not in fake_repo.last_options


@pytest.mark.asyncio
async def test_submit_video_rejects_prompts_without_transcript(fake_repo, fake_bus, tmp_path, fake_user):
    with pytest.raises(Exception):
        await tools.submit_video(
            url="https://x/y", user=fake_user, repo=fake_repo, bus=fake_bus,
            artifacts_root=tmp_path, transcript=False,
            prompts=[{"source": "system", "id": "summary"}],
        )


def test_get_summary_tool_removed():
    assert not hasattr(tools, "get_summary")
```

> Reuse existing MCP test fakes (`fake_repo`, `fake_bus`, `fake_user`) from the current MCP test module; extend `fake_repo` to capture `last_options` if it doesn't already.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_prompts.py -v`
Expected: FAIL — `submit_video` has no `prompts` param / still has `summary`.

- [ ] **Step 3: Write minimal implementation**

In `tools.py` `submit_video`, replace the `summary` handling:

```python
    prompts: list[dict] | None = None,
) -> SubmitVideoResult:
    ...
    from vts.services.prompt_registry import parse_ref, ref_to_dict
    if prompts is None:
        norm = [ref_to_dict("system", "summary")]
    else:
        norm = []
        for entry in prompts:
            source, ref_id = parse_ref(entry)  # raises -> 422 below
            norm.append(ref_to_dict(source, ref_id))
    if norm and not transcript:
        raise HTTPException(status_code=422, detail="prompts require transcript")
    options: dict[str, Any] = {
        "language": language,
        "audio_only": audio_only,
        "transcript": transcript,
        "prompts": norm,
    }
```

Remove the old `summary` param, the `if summary and not transcript` check, and `delete get_summary`. Add the CRUD + `get_prompt_result` tool functions (thin wrappers over `Repo` + `prompt_results.resolve_result_path`). Register/unregister in `server.py`; update the docstring "all six MCP tools" count.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_prompts.py tests/ -k mcp -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/mcp/ tests/test_mcp_prompts.py
git commit -m "feat(prompts): MCP prompts tools, prompts in submit_video, drop get_summary (VOS-63 breaking)"
```

---

## Phase 6 — Frontend: selection, manager, results tab, progress

### Task 11: Prompt selection multiselect on the task form

**Files:**
- Modify: `vts/static/index.html` (replace `#summary` checkbox block ~L182), `vts/static/app.js`, `vts/static/styles.css`, `vts/static/i18n/*.json`
- Test: manual (no JS test harness in repo) — verification steps below.

**Interfaces:**
- Consumes: `GET /api/prompts`.
- Produces: form submits `prompts: [{source,id}]` instead of `summary`. New DOM: a `<div id="prompt-select">` populated from the API; `system:summary` checked by default; empty allowed.

> **DOM-order constraint (memory):** `app.js` has no `defer`; any element referenced by `getElementById` at top level must appear before the `<script>` tag. Place the new markup with the existing form controls (already before the script include), matching the existing pattern used for `#summary`.

- [ ] **Step 1: Replace the checkbox with a multiselect container**

In `index.html`, swap the `#summary` checkbox for:

```html
<div class="field" id="prompt-select-field">
  <span class="field-label" data-i18n="new_task.prompts">Prompts</span>
  <div id="prompt-select" class="prompt-select"></div>
</div>
```

- [ ] **Step 2: Populate it and wire submission in app.js**

Add a loader that fetches `/api/prompts`, renders one checkbox per prompt (label shows name + a `system`/`user` badge), checks `system:summary` by default, and a `getSelectedPrompts()` returning `[{source,id}]`. In the task-create submit handler, replace the `summary` payload field with `prompts: getSelectedPrompts()`.

- [ ] **Step 3: Add i18n keys**

Add `new_task.prompts`, `prompt.system.summary`, `prompt.badge.system`, `prompt.badge.user` to `en.json`, `ru.json`, `de.json`.

- [ ] **Step 4: Verify in the running app**

Run the app (`/run` skill or project launch). Open the task form: confirm the prompt list loads, Summary is checked by default, and creating a task with only custom prompts selected sends `prompts` (check Network tab / created task `options`).

- [ ] **Step 5: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css vts/static/i18n/
git commit -m "feat(prompts): task-form prompt multiselect (VOS-63)"
```

---

### Task 12: Prompt manager panel (CRUD + duplicate)

**Files:**
- Modify: `vts/static/index.html` (top-menu button + modal), `vts/static/app.js`, `vts/static/styles.css`, `vts/static/i18n/*.json`
- Test: manual verification.

**Interfaces:**
- Consumes: `GET/POST/PATCH/DELETE /api/prompts`.
- Produces: a manager modal listing system (read-only) and user prompts; create/edit/delete for user prompts; a **Duplicate** button on every row that opens the create form prefilled with that prompt's `system_prompt` and a copied name. For a system prompt, duplicate must fetch its text — add `GET /api/prompts/{source}/{id}/text` (system: read file; user: read row) OR include `system_prompt` text in a detail call. Implement a small `GET /api/prompts/system/{key}/text` returning the file contents for duplication. (User prompt text comes from a `GET /api/prompts/{id}` detail endpoint — add it returning `system_prompt`.)

> This task adds two small read endpoints in `main.py` (`GET /api/prompts/{prompt_id}` for user detail incl. `system_prompt`; `GET /api/prompts/system/{key}/text`). Add matching tests in `tests/test_prompts_api.py`.

- [ ] **Step 1: Add detail/text endpoints + tests** (TDD, mirror Task 6 style)
- [ ] **Step 2: Add the top-menu button and modal markup** (respect DOM-order constraint)
- [ ] **Step 3: Wire list/create/edit/delete/duplicate in app.js**
- [ ] **Step 4: Add i18n keys** (`prompts.manage.title`, `.create`, `.edit`, `.delete`, `.duplicate`, `.name`, `.body`, `.system_readonly`)
- [ ] **Step 5: Verify in the running app** — create a prompt, edit it, duplicate the system Summary into an editable copy, delete a prompt.
- [ ] **Step 6: Commit**

```bash
git add vts/static/ vts/api/main.py tests/test_prompts_api.py
git commit -m "feat(prompts): prompt manager panel with duplicate (VOS-63)"
```

---

### Task 13: Results tab dropdown + progress with per-prompt steps

**Files:**
- Modify: `vts/static/index.html` (summary tab area ~L384/L418), `vts/static/app.js` (DAG_STEPS, weights, getEnabledSteps, results rendering), `vts/static/i18n/*.json`
- Test: manual verification + existing JS-adjacent assertions if any.

**Interfaces:**
- Consumes: task `options.prompts`, `options.prompt_results`, `GET /api/tasks/{id}/results/{source}/{ref}`.
- Produces:
  - Summary tab → results tab: a `<select id="result-prompt-select">` above the content, populated from `prompt_results` (and selected prompts), filling in as results complete. Selecting an entry loads its text via the result endpoint. Default selection: `system:summary` if present else first ready.
  - Progress: replace the static tail. `getEnabledSteps(task)` builds head + one finalize step per selected prompt (mirror `build_dag_steps`). `STEP_WEIGHT_SECONDS` recalibrated; each finalize step weighted at the current final-call estimate (`estimateFinalSummaryWeight`).

- [ ] **Step 1: Recalibrate weights**

Inspect recent runs' step durations (from task logs/steps) and update `STEP_WEIGHT_SECONDS` + `FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS` constants in `app.js`. Record the source/date in the existing comment.

- [ ] **Step 2: Make `getEnabledSteps` build the dynamic tail**

Replace the `SUMMARY_STEPS`-subtract logic: head steps always (minus summary-head if no prompts), then one finalize step per `options.prompts` entry, named to match server (`summarize_final` for `system:summary`, else `finalize:source:id`). Weight each finalize step via `estimateFinalSummaryWeight`.

- [ ] **Step 3: Build the results dropdown**

Add `#result-prompt-select` markup above `.tab-content.summary`. On task render, populate options from `prompt_results` (name + badge), enable entries whose `status==="completed"`, and load text on change. Keep the default-to-summary behaviour.

- [ ] **Step 4: i18n** — `tab.results`, `results.select_prompt`, `results.pending`.

- [ ] **Step 5: Verify in the running app** — create a task with summary + one custom prompt; watch progress advance through two finalize steps; confirm both results appear in the dropdown and load.

- [ ] **Step 6: Commit**

```bash
git add vts/static/ vts/static/i18n/
git commit -m "feat(prompts): results dropdown + per-prompt progress steps (VOS-63)"
```

---

## Phase 7 — Changelog, version, full suite

### Task 14: CHANGELOG + version bump + breaking-change note + full test run

**Files:**
- Create: `CHANGELOG.md`
- Modify: `vts/__init__.py`

- [ ] **Step 1: Create `CHANGELOG.md`** with a top entry recording: custom prompts feature; **BREAKING (MCP/HTTP):** `summary: bool` removed from task creation in favour of `prompts: [{source,id}]`; MCP `get_summary` removed in favour of `get_prompt_result(task_id, ref)`.

- [ ] **Step 2: Bump version** in `vts/__init__.py` (e.g. `1.0.95` → `1.1.0`, minor for the feature; note breaking-change in changelog regardless).

- [ ] **Step 3: Run the full suite**

Run: `pytest -q`
Expected: PASS (fix any regressions in older tests that referenced `summary`/`get_summary`).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md vts/__init__.py
git commit -m "chore(prompts): CHANGELOG + version bump for custom prompts (VOS-63)"
```

---

## Self-Review Notes

**Spec coverage:**
- Concept/flow (prepared input once, N final calls) → Tasks 8, 9. ✓
- System registry + i18n names → Tasks 1, 6, 11/12. ✓
- `prompts` table + per-user CRUD → Tasks 3, 4, 6. ✓
- `PromptRef {source,id}` everywhere → Tasks 1, 5, 8, 9, 10. ✓
- Results as files + `prompt_results` index (in `options`) → Tasks 7, 9. ✓
- Built-in summary keeps `summary_path`/`final.md` → Tasks 7, 9 (explicit). ✓
- HTTP drop `summary`, add `prompts`, result endpoint → Tasks 5, 7. ✓
- MCP drop `summary`/`get_summary`, add tools + `get_prompt_result` → Task 10. ✓
- UI multiselect / manager+duplicate / results dropdown / per-prompt progress → Tasks 11, 12, 13. ✓
- Legacy `summary` bool back-compat → Task 2 (`selected_prompt_refs`). ✓
- Changelog + breaking-change note + version → Task 14. ✓
- Follow-ups (presets, dynamic metrics) → already filed in bd (vts-hp7, vts-b6t), out of scope. ✓

**Open implementation detail (flagged inline, not a blocker):** exact processor step-dispatch site (handler map vs if/elif at `processor.py:243`) — Task 8/9 instruct to follow whatever dispatch shape exists; the implementer reads that section before editing.

**Type consistency:** `PromptRef`/`{source,id}`, `finalize_step_name`, `selected_prompt_refs`, `resolve_result_path`, `upsert_result_entry` names are used consistently across tasks.
