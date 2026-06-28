# Task Option Presets (vts-hp7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users save a named bundle of task-creation options (language, audio_only, transcript, prompts) as a preset and apply it when creating a task — with a system read-only "Default" preset, per-user default-preset selection, a manager dialog, and MCP support.

**Architecture:** Mirrors the custom-prompts feature (VOS-63). A `preset_registry` defines system presets; a `presets` table holds per-user presets; `users.default_preset` (JSON) points at the active default (system or user). HTTP exposes preset CRUD + default get/set; the web UI applies presets client-side (a dropdown fills the create form). MCP gets preset CRUD + a `preset` param on task creation that the server expands server-side. `TaskCreateRequest` is unchanged.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async + Postgres (tests via in-test Postgres), FastMCP, pytest; vanilla JS frontend; verifier-web (Playwright) for UI.

**Spec:** [docs/superpowers/specs/2026-06-28-task-presets-design.md](../specs/2026-06-28-task-presets-design.md)

## Global Constraints

- Version bump in `vts/__init__.py` before the final commit (patch).
- `PresetRef` shape fixed everywhere: `{"source": "system"|"user", "id": str}` (id = registry key for system, UUID str for user). Mirror `vts.api.schemas.PromptRef`.
- Preset `options` shape: `{"language": str|None, "audio_only": bool, "transcript": bool, "prompts": [PromptRef]}`.
- System preset registry: exactly one entry initially — key `default`, i18n key `preset.system.default`, display_name `"Default"`, options `{language:null, audio_only:false, transcript:true, prompts:[{source:"system",id:"summary"}]}`.
- `users.default_preset`: new JSON nullable column storing a `PresetRef` or null (null → system "default").
- Deleting the user's active-default preset → reset default to the system "default" (first system preset if several).
- System preset `name` returned by the API is the English `display_name`; the web UI localizes by id (key `preset.system.${id}`) — same approach as prompts (vts-mqk). Mirror `promptDisplayName`.
- Applying a preset is CLIENT-SIDE in the UI; `TaskCreateRequest` is NOT changed. MCP `submit_video`/`create_task` gets a `preset` param the SERVER expands (preset options as base, explicit fields override; dangling user-prompt refs filtered).
- Dangling user-prompt refs in a preset's `options.prompts` are silently filtered on apply/expand; system refs always valid.
- Migrations are Alembic; new revision chains from `0010_prompts` → `0011_presets`.
- Tests run on real Postgres: bring up a throwaway PG and set `VTS_TEST_DATABASE_URL=postgresql+asyncpg://vts:vts@localhost:5432/vts_test` (podman run postgres:16, wait pg_isready, run, remove). Use the `client`/`authed_app` harness in `tests/conftest.py` for HTTP, the `session` fixture in `tests/test_prompts_repo.py` for repo tests.
- Reuse: `renderPromptMultiselect` (app.js), the `tokens-dialog` CSS/markup pattern, `parse_ref`-style helpers.

---

## File Structure

**New:**
- `vts/services/preset_registry.py` — `SystemPresetDef`, `SYSTEM_PRESETS`, `list_system_presets`, `system_preset_keys`, `parse_preset_ref`, `preset_ref_to_dict`, `default_system_preset()`.
- `vts/services/preset_expand.py` — `expand_preset_options(preset_options, available_prompt_refs)` (filter dangling) + `resolve_preset(source, id, repo, user_id)`.
- `alembic/versions/0011_presets.py` — `presets` table + `users.default_preset` column.
- Tests: `tests/test_preset_registry.py`, `tests/test_presets_repo.py`, `tests/test_presets_api.py`, `tests/test_mcp_presets.py`, `tests/ui/scenarios/preset-select.mjs`.

**Modified:**
- `vts/db/models.py` — `Preset` model + `User.default_preset` column.
- `vts/db/repo.py` — preset CRUD + `get_user_default_preset` / `set_user_default_preset` + dangling-default reset on delete.
- `vts/api/schemas.py` — `PresetRef`, `PresetOptions`, `PresetOut`, `PresetCreateRequest`, `PresetUpdateRequest`.
- `vts/api/main.py` — preset CRUD endpoints + default get/set.
- `vts/mcp/tools.py` + `vts/mcp/server.py` + `vts/mcp/schemas.py` — preset tools + `preset` on submit_video.
- `vts/static/index.html`, `app.js`, `styles.css`, `i18n/{en,ru,de}.js` — dropdown, save button, manager dialog, dangling hint.
- `CHANGELOG.md`, `vts/__init__.py`.

---

## Phase 1 — Registry + ref helpers + expand

### Task 1: Preset registry + ref helpers

**Files:**
- Create: `vts/services/preset_registry.py`
- Test: `tests/test_preset_registry.py`

**Interfaces:**
- Produces:
  - `SystemPresetDef` frozen dataclass `(key: str, i18n_name_key: str, display_name: str, options: dict)`.
  - `SYSTEM_PRESETS: list[SystemPresetDef]` — one entry: `default`.
  - `list_system_presets() -> list[SystemPresetDef]`, `system_preset_keys() -> set[str]`, `default_system_preset() -> SystemPresetDef` (the first system preset).
  - `parse_preset_ref(value: dict|str) -> tuple[str,str]` (raises ValueError on bad source/empty id), `preset_ref_to_dict(source,id) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preset_registry.py
import pytest
from vts.services.preset_registry import (
    SYSTEM_PRESETS, list_system_presets, system_preset_keys, default_system_preset,
    parse_preset_ref, preset_ref_to_dict,
)

def test_default_preset_registered():
    keys = system_preset_keys()
    assert "default" in keys
    d = default_system_preset()
    assert d.key == "default"
    assert d.display_name == "Default"
    assert d.i18n_name_key == "preset.system.default"
    assert d.options == {
        "language": None, "audio_only": False, "transcript": True,
        "prompts": [{"source": "system", "id": "summary"}],
    }

def test_parse_preset_ref_dict_and_string():
    assert parse_preset_ref({"source": "user", "id": "abc"}) == ("user", "abc")
    assert parse_preset_ref("system:default") == ("system", "default")

def test_parse_preset_ref_rejects_bad():
    with pytest.raises(ValueError):
        parse_preset_ref({"source": "nope", "id": "x"})
    with pytest.raises(ValueError):
        parse_preset_ref({"source": "user", "id": ""})

def test_ref_to_dict():
    assert preset_ref_to_dict("system", "default") == {"source": "system", "id": "default"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_preset_registry.py -v`
Expected: FAIL `ModuleNotFoundError: vts.services.preset_registry`

- [ ] **Step 3: Implement**

```python
# vts/services/preset_registry.py
from __future__ import annotations
from dataclasses import dataclass

VALID_SOURCES = {"system", "user"}

@dataclass(frozen=True)
class SystemPresetDef:
    key: str
    i18n_name_key: str
    display_name: str
    options: dict

SYSTEM_PRESETS: list[SystemPresetDef] = [
    SystemPresetDef(
        key="default",
        i18n_name_key="preset.system.default",
        display_name="Default",
        options={
            "language": None, "audio_only": False, "transcript": True,
            "prompts": [{"source": "system", "id": "summary"}],
        },
    ),
]

def list_system_presets() -> list[SystemPresetDef]:
    return list(SYSTEM_PRESETS)

def system_preset_keys() -> set[str]:
    return {p.key for p in SYSTEM_PRESETS}

def default_system_preset() -> SystemPresetDef:
    return SYSTEM_PRESETS[0]

def parse_preset_ref(value: dict | str) -> tuple[str, str]:
    if isinstance(value, str):
        source, _, ref_id = value.partition(":")
    elif isinstance(value, dict):
        source = str(value.get("source", ""))
        ref_id = str(value.get("id", ""))
    else:
        raise ValueError(f"invalid preset ref: {value!r}")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid preset source: {source!r}")
    if not ref_id:
        raise ValueError("preset ref id must not be empty")
    return source, ref_id

def preset_ref_to_dict(source: str, id: str) -> dict:
    return {"source": source, "id": id}
```

- [ ] **Step 4: Run to verify it passes** — same command. Expected PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add vts/services/preset_registry.py tests/test_preset_registry.py
git commit -m "feat(presets): system preset registry + ref helpers (vts-hp7)"
```

---

### Task 2: Preset-options expansion (dangling-prompt filter)

**Files:**
- Create: `vts/services/preset_expand.py`
- Test: `tests/test_preset_registry.py` (extend; pure function)

**Interfaces:**
- Consumes: nothing project-specific (pure).
- Produces:
  - `filter_prompt_refs(prompts: list[dict], valid_user_ids: set[str]) -> list[dict]` — keeps all `system` refs and only `user` refs whose id ∈ valid_user_ids.
  - `expand_preset_options(options: dict, valid_user_prompt_ids: set[str]) -> dict` — returns a copy of `options` with `prompts` filtered via `filter_prompt_refs`; missing keys defaulted (`language:None, audio_only:False, transcript:True, prompts:[]`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preset_registry.py (append)
from vts.services.preset_expand import filter_prompt_refs, expand_preset_options

def test_filter_keeps_system_drops_unknown_user():
    refs = [{"source":"system","id":"summary"},
            {"source":"user","id":"keep"},
            {"source":"user","id":"gone"}]
    assert filter_prompt_refs(refs, {"keep"}) == [
        {"source":"system","id":"summary"}, {"source":"user","id":"keep"}]

def test_expand_defaults_missing_and_filters():
    opts = {"audio_only": True, "prompts": [{"source":"user","id":"gone"}]}
    out = expand_preset_options(opts, set())
    assert out == {"language": None, "audio_only": True, "transcript": True, "prompts": []}
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_preset_registry.py -k expand or filter -v`. Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# vts/services/preset_expand.py
from __future__ import annotations
from typing import Any

def filter_prompt_refs(prompts: list[dict], valid_user_ids: set[str]) -> list[dict]:
    out: list[dict] = []
    for r in prompts or []:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if src == "system":
            out.append({"source": "system", "id": str(r.get("id"))})
        elif src == "user" and str(r.get("id")) in valid_user_ids:
            out.append({"source": "user", "id": str(r.get("id"))})
    return out

def expand_preset_options(options: dict, valid_user_prompt_ids: set[str]) -> dict[str, Any]:
    o = options or {}
    return {
        "language": o.get("language"),
        "audio_only": bool(o.get("audio_only", False)),
        "transcript": bool(o.get("transcript", True)),
        "prompts": filter_prompt_refs(o.get("prompts", []), valid_user_prompt_ids),
    }
```

- [ ] **Step 4: Run to verify passes.** **Step 5: Commit**

```bash
git add vts/services/preset_expand.py tests/test_preset_registry.py
git commit -m "feat(presets): preset options expansion with dangling-prompt filter (vts-hp7)"
```

---

## Phase 2 — DB

### Task 3: Preset model + User.default_preset + migration 0011

**Files:**
- Modify: `vts/db/models.py` (add `Preset` after `Prompt`; add `default_preset` to `User`)
- Create: `alembic/versions/0011_presets.py`
- Test: `tests/test_presets_repo.py` (model-import smoke)

**Interfaces:**
- Produces: `vts.db.models.Preset` (id, user_id, name, options JSON, created_at, updated_at); `User.default_preset` JSON nullable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presets_repo.py
from vts.db.models import Preset, User

def test_preset_model_columns():
    cols = set(Preset.__table__.columns.keys())
    assert {"id","user_id","name","options","created_at","updated_at"} <= cols

def test_user_has_default_preset_column():
    assert "default_preset" in set(User.__table__.columns.keys())
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_presets_repo.py -v`. Expected ImportError/AttributeError.

- [ ] **Step 3: Implement**

In `vts/db/models.py`, add to `User` (after `preferred_ytdlp_client`):
```python
    default_preset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```
Add the model (after `Prompt`):
```python
class Preset(Base):
    __tablename__ = "presets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_presets_user_created", "user_id", "created_at"),
    )
```
(`JSON`, `String`, `Index`, `ForeignKey`, `Any`, `datetime`, `utcnow`, `UUID` already imported in models.py.)

Create `alembic/versions/0011_presets.py`:
```python
"""Add presets table and users.default_preset (vts-hp7)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_presets"
down_revision = "0010_prompts"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_presets_user_created", "presets", ["user_id", "created_at"])
    op.add_column("users", sa.Column("default_preset", sa.JSON(), nullable=True))

def downgrade() -> None:
    op.drop_column("users", "default_preset")
    op.drop_index("ix_presets_user_created", table_name="presets")
    op.drop_table("presets")
```

- [ ] **Step 4: Run to verify passes** (real Postgres not needed — model-import test). **Step 5: Commit**

```bash
git add vts/db/models.py alembic/versions/0011_presets.py tests/test_presets_repo.py
git commit -m "feat(presets): Preset model + users.default_preset + 0011 migration (vts-hp7)"
```

---

### Task 4: Repo — preset CRUD + default get/set + delete-resets-default

**Files:**
- Modify: `vts/db/repo.py` (after the prompt CRUD methods)
- Test: `tests/test_presets_repo.py` (extend; needs Postgres)

**Interfaces:**
- Produces on `Repo`:
  - `create_preset(user_id, name, options) -> Preset`
  - `list_presets(user_id) -> list[Preset]` (newest first)
  - `get_preset(user_id, preset_id) -> Preset | None`
  - `update_preset(user_id, preset_id, *, name: str|None, options: dict|None) -> Preset | None`
  - `delete_preset(user_id, preset_id) -> bool` — if the deleted preset was the user's default (`default_preset == {"source":"user","id":str(preset_id)}`), reset `user.default_preset = None`.
  - `get_user_default_preset(user_id) -> dict | None`
  - `set_user_default_preset(user_id, ref: dict | None) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presets_repo.py (append; reuse the `session` fixture from test_prompts_repo style)
import uuid
import pytest
from vts.db.repo import Repo
from vts.db.models import User

async def _user(session):
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u); await session.flush(); return u.id

@pytest.mark.asyncio
async def test_preset_crud_and_default(session):
    repo = Repo(session); uid = await _user(session)
    opts = {"language": None, "audio_only": False, "transcript": True,
            "prompts": [{"source":"system","id":"summary"}]}
    p = await repo.create_preset(uid, "Mine", opts)
    assert (await repo.list_presets(uid))[0].id == p.id
    assert (await repo.get_preset(uid, p.id)).name == "Mine"
    upd = await repo.update_preset(uid, p.id, name="Renamed", options=None)
    assert upd.name == "Renamed" and upd.options == opts

    # default get/set
    assert await repo.get_user_default_preset(uid) is None
    await repo.set_user_default_preset(uid, {"source":"user","id":str(p.id)})
    assert await repo.get_user_default_preset(uid) == {"source":"user","id":str(p.id)}

    # deleting the default preset resets default to None
    assert await repo.delete_preset(uid, p.id) is True
    assert await repo.get_user_default_preset(uid) is None

@pytest.mark.asyncio
async def test_preset_isolation(session):
    repo = Repo(session); a = await _user(session); b = await _user(session)
    p = await repo.create_preset(a, "A", {})
    assert await repo.get_preset(b, p.id) is None
    assert await repo.delete_preset(b, p.id) is False
    assert await repo.update_preset(b, p.id, name="X", options=None) is None
```

- [ ] **Step 2: Run (Postgres up) to verify fails** — `AttributeError: create_preset`.

- [ ] **Step 3: Implement** (mirror prompt CRUD; `Preset` added to the models import line):

```python
    async def create_preset(self, user_id: uuid.UUID, name: str, options: dict) -> Preset:
        preset = Preset(user_id=user_id, name=name, options=options)
        self.session.add(preset)
        await self.session.flush()
        return preset

    async def list_presets(self, user_id: uuid.UUID) -> list[Preset]:
        stmt = select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at.desc())
        return list(await self.session.scalars(stmt))

    async def get_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> Preset | None:
        return await self.session.scalar(
            select(Preset).where(Preset.id == preset_id, Preset.user_id == user_id))

    async def update_preset(self, user_id, preset_id, *, name, options) -> Preset | None:
        preset = await self.get_preset(user_id, preset_id)
        if preset is None:
            return None
        if name is not None:
            preset.name = name
        if options is not None:
            preset.options = options
        await self.session.flush()
        return preset

    async def get_user_default_preset(self, user_id: uuid.UUID) -> dict | None:
        u = await self.session.scalar(select(User).where(User.id == user_id))
        return u.default_preset if u else None

    async def set_user_default_preset(self, user_id: uuid.UUID, ref: dict | None) -> None:
        u = await self.session.scalar(select(User).where(User.id == user_id))
        if u is not None:
            u.default_preset = ref
            await self.session.flush()

    async def delete_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> bool:
        preset = await self.get_preset(user_id, preset_id)
        if preset is None:
            return False
        u = await self.session.scalar(select(User).where(User.id == user_id))
        if u is not None and u.default_preset == {"source": "user", "id": str(preset_id)}:
            u.default_preset = None
        await self.session.delete(preset)
        await self.session.flush()
        return True
```

- [ ] **Step 4: Run to verify passes** (Postgres). **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_presets_repo.py
git commit -m "feat(presets): repo CRUD + default get/set + delete-resets-default (vts-hp7)"
```

---

## Phase 3 — HTTP API

### Task 5: Schemas

**Files:**
- Modify: `vts/api/schemas.py`
- Test: `tests/test_presets_api.py` (schema-level)

**Interfaces:**
- Produces:
  - `PresetRef(BaseModel)`: `source: Literal["system","user"]`, `id: str = Field(min_length=1)`.
  - `PresetOptions(BaseModel)`: `language: str|None = None`, `audio_only: bool = False`, `transcript: bool = True`, `prompts: list[PromptRef] = []`.
  - `PresetOut(BaseModel)`: `source, id, name, options: PresetOptions, editable: bool`.
  - `PresetCreateRequest`: `name: str = Field(min_length=1, max_length=255)`, `options: PresetOptions`.
  - `PresetUpdateRequest`: `name: str|None`, `options: PresetOptions|None` (name blank-after-strip rejected via validator, mirroring PromptUpdateRequest).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presets_api.py
import pytest
from pydantic import ValidationError
from vts.api.schemas import PresetRef, PresetOptions, PresetCreateRequest, PresetUpdateRequest

def test_preset_options_defaults():
    o = PresetOptions()
    assert o.language is None and o.audio_only is False and o.transcript is True and o.prompts == []

def test_preset_create_validates():
    with pytest.raises(ValidationError):
        PresetCreateRequest(name="", options=PresetOptions())

def test_preset_update_blank_name_rejected():
    with pytest.raises(ValidationError):
        PresetUpdateRequest(name="   ")
```

- [ ] **Step 2: Run to verify fails.** **Step 3: Implement** (mirror PromptRef/PromptUpdateRequest; `Literal`/`Field`/`model_validator`/`BaseModel` already imported; `PromptRef` already defined in this module):

```python
class PresetRef(BaseModel):
    source: Literal["system", "user"]
    id: str = Field(min_length=1)

class PresetOptions(BaseModel):
    language: str | None = None
    audio_only: bool = False
    transcript: bool = True
    prompts: list[PromptRef] = Field(default_factory=list)

class PresetOut(BaseModel):
    source: str
    id: str
    name: str
    options: PresetOptions
    editable: bool

class PresetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    options: PresetOptions

class PresetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    options: PresetOptions | None = None

    @model_validator(mode="after")
    def _validate_name(self) -> "PresetUpdateRequest":
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip() if self.name is not None else None
        return self
```

- [ ] **Step 4: Run to verify passes.** **Step 5: Commit**

```bash
git add vts/api/schemas.py tests/test_presets_api.py
git commit -m "feat(presets): API schemas (vts-hp7)"
```

---

### Task 6: HTTP endpoints — preset CRUD + default get/set

**Files:**
- Modify: `vts/api/main.py` (after the prompt endpoints, ~L1190)
- Test: `tests/test_presets_api.py` (extend; Postgres + `client` harness)

**Interfaces:**
- Consumes: repo preset methods, `list_system_presets`, `default_system_preset`, `parse_preset_ref`, `PresetOut/PresetOptions/PresetCreateRequest/PresetUpdateRequest`.
- Produces HTTP:
  - `GET /api/presets` → `list[PresetOut]` (system first incl. `default` editable=false with its registry options; then user newest-first; system `name` = display_name).
  - `POST /api/presets` → `PresetOut` (user).
  - `PATCH /api/presets/{preset_id}` → `PresetOut` (404 if missing).
  - `DELETE /api/presets/{preset_id}` → 204 (404 if missing; resets default if it was default).
  - `GET /api/me/default_preset` → `{source,id}` (the user's default, or `{"source":"system","id":<default_system_key>}` if null).
  - `PUT /api/me/default_preset` (body `{source,id}`) → 204. Validate: system ref must be a known key; user ref must be an existing user preset → else 404. Store via `set_user_default_preset` (store `None` if it equals the system default? No — store the ref as given; for system store `{"source":"system","id":key}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presets_api.py (append; reuse `client` authed harness)
import pytest

@pytest.mark.asyncio
async def test_presets_list_includes_system_default(client):
    body = (await client.get("/api/presets")).json()
    sys = next(p for p in body if p["source"]=="system" and p["id"]=="default")
    assert sys["editable"] is False
    assert sys["options"]["transcript"] is True
    assert sys["options"]["prompts"] == [{"source":"system","id":"summary"}]

@pytest.mark.asyncio
async def test_preset_crud_and_default_endpoints(client):
    created = (await client.post("/api/presets", json={
        "name":"Mine","options":{"language":"en","audio_only":True,"transcript":True,
        "prompts":[{"source":"system","id":"summary"}]}})).json()
    pid = created["id"]; assert created["source"]=="user" and created["editable"] is True
    # default starts as system
    assert (await client.get("/api/me/default_preset")).json() == {"source":"system","id":"default"}
    # set user default
    assert (await client.put("/api/me/default_preset", json={"source":"user","id":pid})).status_code == 204
    assert (await client.get("/api/me/default_preset")).json() == {"source":"user","id":pid}
    # set unknown user default -> 404
    import uuid
    assert (await client.put("/api/me/default_preset", json={"source":"user","id":str(uuid.uuid4())})).status_code == 404
    # delete the default preset -> default falls back to system
    assert (await client.delete(f"/api/presets/{pid}")).status_code == 204
    assert (await client.get("/api/me/default_preset")).json() == {"source":"system","id":"default"}
```

- [ ] **Step 2: Run (Postgres) to verify fails** (404 routes). **Step 3: Implement** (mirror prompt endpoints):

```python
    @app.get("/api/presets", response_model=list[PresetOut])
    async def list_presets_endpoint(user: AuthenticatedUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session_dep)) -> list[PresetOut]:
        from vts.services.preset_registry import list_system_presets
        out = [PresetOut(source="system", id=p.key, name=p.display_name,
                         options=PresetOptions(**p.options), editable=False)
               for p in list_system_presets()]
        repo = Repo(session)
        for row in await repo.list_presets(uuid.UUID(user.id)):
            out.append(PresetOut(source="user", id=str(row.id), name=row.name,
                                 options=PresetOptions(**row.options), editable=True))
        return out

    @app.post("/api/presets", response_model=PresetOut)
    async def create_preset_endpoint(payload: PresetCreateRequest,
                                     user: AuthenticatedUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session_dep)) -> PresetOut:
        repo = Repo(session)
        row = await repo.create_preset(uuid.UUID(user.id), payload.name.strip(), payload.options.model_dump())
        await session.commit()
        return PresetOut(source="user", id=str(row.id), name=row.name,
                         options=PresetOptions(**row.options), editable=True)

    @app.patch("/api/presets/{preset_id}", response_model=PresetOut)
    async def update_preset_endpoint(preset_id: uuid.UUID, payload: PresetUpdateRequest,
                                     user: AuthenticatedUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session_dep)) -> PresetOut:
        repo = Repo(session)
        row = await repo.update_preset(uuid.UUID(user.id), preset_id,
                                       name=payload.name,
                                       options=payload.options.model_dump() if payload.options else None)
        if row is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        await session.commit()
        return PresetOut(source="user", id=str(row.id), name=row.name,
                         options=PresetOptions(**row.options), editable=True)

    @app.delete("/api/presets/{preset_id}", status_code=204)
    async def delete_preset_endpoint(preset_id: uuid.UUID,
                                     user: AuthenticatedUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session_dep)) -> Response:
        repo = Repo(session)
        if not await repo.delete_preset(uuid.UUID(user.id), preset_id):
            raise HTTPException(status_code=404, detail="Preset not found")
        await session.commit()
        return Response(status_code=204)

    @app.get("/api/me/default_preset")
    async def get_default_preset_endpoint(user: AuthenticatedUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session_dep)) -> dict:
        from vts.services.preset_registry import default_system_preset
        repo = Repo(session)
        ref = await repo.get_user_default_preset(uuid.UUID(user.id))
        return ref or {"source": "system", "id": default_system_preset().key}

    @app.put("/api/me/default_preset", status_code=204)
    async def set_default_preset_endpoint(payload: PresetRef,
                                          user: AuthenticatedUser = Depends(get_current_user),
                                          session: AsyncSession = Depends(get_session_dep)) -> Response:
        from vts.services.preset_registry import system_preset_keys
        repo = Repo(session)
        if payload.source == "system":
            if payload.id not in system_preset_keys():
                raise HTTPException(status_code=404, detail="Unknown system preset")
        else:
            if await repo.get_preset(uuid.UUID(user.id), uuid.UUID(payload.id)) is None:
                raise HTTPException(status_code=404, detail="Preset not found")
        await repo.set_user_default_preset(uuid.UUID(user.id), {"source": payload.source, "id": payload.id})
        await session.commit()
        return Response(status_code=204)
```

Add `PresetOut, PresetOptions, PresetRef, PresetCreateRequest, PresetUpdateRequest` to the schemas import in main.py.

- [ ] **Step 4: Run to verify passes** (Postgres) + full suite. **Step 5: Commit**

```bash
git add vts/api/main.py tests/test_presets_api.py
git commit -m "feat(presets): HTTP CRUD + default get/set endpoints (vts-hp7)"
```

---

## Phase 4 — MCP

### Task 7: MCP preset tools + `preset` on submit_video

**Files:**
- Modify: `vts/mcp/tools.py`, `vts/mcp/server.py`, `vts/mcp/schemas.py`
- Test: `tests/test_mcp_presets.py`

**Interfaces:**
- Consumes: repo preset methods, registry, `expand_preset_options`, `resolve_preset`.
- Produces: tools `list_presets`, `create_preset`, `update_preset`, `delete_preset`, `get_default_preset`, `set_default_preset`; and `submit_video` gains `preset: dict | None = None`.

`resolve_preset` (add to `vts/services/preset_expand.py`): given `(source, id, repo, user_id)`, return the preset's raw `options` dict (system → registry options; user → row.options; None if not found).

`submit_video` preset handling: if `preset` given, `parse_preset_ref` it, resolve options, compute `valid_user_prompt_ids` (the user's prompt ids), `expand_preset_options(...)` as the base; then explicit params (`language`, `transcript`, `audio_only`, `prompts`) that were passed override the base. (Detect "passed" via sentinel defaults — simplest: the base is applied, then for each field only override if the caller's value differs from the function default AND was explicitly provided. To keep it simple and unambiguous: `preset` provides defaults ONLY for fields the caller left at their default. Document this.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_presets.py
import pytest
from vts.mcp import tools

@pytest.mark.asyncio
async def test_submit_video_with_preset_expands_options(fake_repo_with_preset, fake_bus, tmp_path, fake_user):
    # preset has language="en", transcript=True, prompts=[summary]; caller passes only a preset ref
    res = await tools.submit_video(url="https://x/y", user=fake_user, repo=fake_repo_with_preset,
                                   bus=fake_bus, artifacts_root=tmp_path,
                                   preset={"source":"user","id":fake_repo_with_preset.preset_id})
    opts = fake_repo_with_preset.last_options
    assert opts["language"] == "en"
    assert opts["prompts"] == [{"source":"system","id":"summary"}]
```

(Extend the MCP `FakeRepo` to back presets + capture `last_options`. Mirror the existing prompt MCP test fakes in `tests/mcp/`.)

- [ ] **Step 2–5:** implement the tools + submit_video preset expansion mirroring the prompt-tool wrappers in `tools.py`/`server.py`; register in server.py; run `pytest tests/test_mcp_presets.py tests/mcp/ -v` then full suite; commit.

```bash
git commit -m "feat(presets): MCP preset tools + preset expansion in submit_video (vts-hp7)"
```

---

## Phase 5 — Frontend

### Task 8: Preset dropdown + apply + save button on the create form

**Files:**
- Modify: `vts/static/index.html`, `app.js`, `styles.css`, `i18n/{en,ru,de}.js`
- Test: manual + `node --check`; verifier-web scenario in Task 10.

**Interfaces:**
- Consumes: `GET /api/presets`, `GET /api/me/default_preset`, `POST/PATCH /api/presets`, `renderPromptMultiselect`, `getSelectedFrom`.

- [ ] **Step 1: Markup** — add a preset dropdown `<select id="preset-select">` above/left of the option controls (inside the new-task form, before the script tags — DOM-order rule), and a save button `<button id="preset-save-btn">`. i18n label `new_task.preset`.
- [ ] **Step 2: Load + apply** — on init, `GET /api/presets` (populate dropdown; system localized via `t("preset.system."+id)` fallback to name) and `GET /api/me/default_preset` (select it, apply its options to the form: set language, audio_only, transcript, and re-render the prompt multiselect with the preset's filtered prompts). Selecting another preset re-applies.
- [ ] **Step 3: Apply = fill form** — a `applyPresetOptions(options)` that sets the four controls; filter dangling user-prompt refs against the loaded `/api/prompts` list before checking the multiselect.
- [ ] **Step 4: Dirty tracking + save button states** — track whether the form differs from the selected preset's options. Button label: no preset / clean → `preset.save_as` ("Save as preset", prompts for a name → POST); user preset + dirty → `preset.save_changes` ("Save changes" → PATCH selected); system preset + dirty → `preset.save_as` only. After save, refresh the dropdown + clear dirty.
- [ ] **Step 5: Dangling hint** — when applying a preset whose prompts contain refs not in `/api/prompts`, show a small hint (`preset.dangling_prompts`) with a `preset.resave` ("Re-save") button that PATCHes the preset with filtered prompts.
- [ ] **Step 6: i18n + node --check + verify in app.** **Commit.**

```bash
git commit -m "feat(presets): create-form preset dropdown, apply, save button (vts-hp7)"
```

---

### Task 9: Preset manager dialog (CRUD + duplicate + make-default)

**Files:**
- Modify: `vts/static/index.html` (button + `<dialog id="presets-dialog">`), `app.js`, `styles.css`, `i18n/*.js`
- Test: manual + `node --check`; verifier-web in Task 10.

- [ ] **Step 1:** header button `#presets-btn` + `<dialog id="presets-dialog" class="tokens-dialog">` (mirror prompts-dialog), before script tags.
- [ ] **Step 2:** list system (read-only) + user presets; per row: user → edit/delete; all → Duplicate (POST with name+" (copy)" and options; system→user) and "Make default" (`PUT /api/me/default_preset`).
- [ ] **Step 3:** edit form for a user preset: name + option controls (language/audio_only/transcript + `renderPromptMultiselect` flat). Save → PATCH.
- [ ] **Step 4:** i18n keys `preset.manage.*`, `preset.copy_suffix`, `preset.make_default`. node --check. Verify in app. **Commit.**

```bash
git commit -m "feat(presets): preset manager dialog with duplicate + make-default (vts-hp7)"
```

---

### Task 10: verifier-web scenario + CHANGELOG + version + full suite

**Files:**
- Create: `tests/ui/scenarios/preset-select.mjs`
- Modify: `CHANGELOG.md`, `vts/__init__.py`

- [ ] **Step 1: verifier scenario** — `tests/ui/scenarios/preset-select.mjs` using the harness: override `/api/presets` (system default + one user preset) and `/api/me/default_preset`; assert the dropdown renders, the default preset is applied to the form on load (e.g. the transcript checkbox state matches), selecting the user preset re-applies its options, and the manager dialog (`#presets-dialog`) is hidden when closed + opens from `#presets-btn`. Black-box, assert closed-state first. Run `cd tests/ui && node run.mjs` → all PASS.
- [ ] **Step 2: CHANGELOG** entry: task option presets (named option bundles, system "Default", per-user default, manager dialog, MCP support).
- [ ] **Step 3: version bump** (patch) in `vts/__init__.py`.
- [ ] **Step 4: full Python suite** (Postgres) → PASS.
- [ ] **Step 5: Commit.**

```bash
git add tests/ui/scenarios/preset-select.mjs CHANGELOG.md vts/__init__.py
git commit -m "feat(presets): verifier-web scenario + changelog + version (vts-hp7)"
```

---

## Self-Review Notes

**Spec coverage:**
- Concept (named option bundle; system/user; one active default) → Tasks 1,3,4,6. ✓
- Data model (presets table, users.default_preset, registry, PresetRef, prompts in options) → Tasks 1,3,5. ✓
- Dangling-prompt filter on apply/expand → Task 2; UI re-save → Task 8. ✓
- HTTP CRUD + default get/set; delete-resets-default; TaskCreateRequest unchanged → Tasks 4,6. ✓
- System name English + UI localize-by-id → Tasks 6 (display_name) + 8 (t by id). ✓
- MCP CRUD + preset on submit_video (server expands, explicit overrides) → Task 7. ✓
- UI: dropdown + apply default on load, one save button (3 states), manager dialog (CRUD+duplicate+make-default), dangling hint+Re-save → Tasks 8,9. ✓
- Testing incl. verifier-web UI scenario → Task 10. ✓

**Placeholder scan:** Tasks 1–6 carry complete code. Tasks 7–9 (MCP wrappers + frontend) are step-described with the exact endpoints/inputs/outputs and "mirror <named existing symbol>" pointers (the prompt tools/dialog are the concrete templates) rather than full transcription — consistent with how the prompts plan handled the parallel MCP/UI tasks; each step names the real surface to mirror, not a vague placeholder.

**Type consistency:** `PresetRef`/`{source,id}`, `PresetOptions` (language/audio_only/transcript/prompts), `expand_preset_options`, `resolve_preset`, repo `create_preset/list_presets/get_preset/update_preset/delete_preset/get_user_default_preset/set_user_default_preset`, endpoint paths `/api/presets` + `/api/me/default_preset` — used consistently across tasks.
