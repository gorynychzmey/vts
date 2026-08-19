# Editable System Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each user edit the final-summary system prompt and put the vendor's version back, with untouched copies picking up a newly shipped prompt automatically.

**Architecture:** `global_prompt.md` stops being read directly and becomes a per-user row in `prompts`, created on first use by a service function both the API and the pipeline call. `updated_at` becomes the record of whether the user has edited their copy — `NULL` means never — which is what lets a startup pass refresh untouched copies without disturbing edited ones. Restoring is the existing `DELETE`: the row goes, the next request recreates it from the file.

**Tech Stack:** Python 3.14, SQLAlchemy 2 (async), Alembic, FastAPI, pytest, vanilla JS frontend.

**Spec:** `docs/superpowers/specs/2026-08-19-editable-system-prompt-design.md`

## Global Constraints

- Python 3.14; run tests with the worktree's own `.venv/bin/python -m pytest` (the project uses `requirements.txt`, not uv).
- The full suite needs a local Postgres: `sudo podman start vts-test-pg`.
- Comments, docstrings and commit messages in English; chat and bd notes in Russian.
- Bump `__version__` in `vts/__init__.py` in the same commit as the change it ships (concurrent sessions share this repo — a lone `chore: bump` commit is how a tag ends up on a commit CI never gated).
- Commit narrow paths — never `git add -A` or `git commit -a`. Another agent may hold uncommitted work in this repo.
- `docs/ui-inventory.md` is generated: run `make ui-inventory`, never hand-edit.
- UI strings go in all three locales: `vts/static/i18n/{en,ru,de}.js`.

---

### Task 1: `is_system` column and nullable `updated_at`

**Files:**
- Modify: `vts/db/models.py:181-197` (`Prompt`)
- Create: `alembic/versions/<rev>_prompt_is_system.py`
- Test: `tests/test_prompt_system_copy.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Prompt.is_system: Mapped[bool]` — `nullable=False`, `default=False`
  - `Prompt.updated_at: Mapped[datetime | None]` — nullable, **no** `default`, **no** `onupdate`
  - partial unique index `ix_prompts_one_system_per_user` on `(user_id) WHERE is_system`

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompt_system_copy.py`:

```python
"""Behaviour of the per-user copy of a vendor system prompt (vts-kujy)."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa

from vts.db.models import Prompt


@pytest.mark.asyncio
async def test_system_copy_is_created_without_an_updated_at(factory) -> None:
    """`updated_at` answers "when did the user change this?" — NULL means never.

    A plain `session.add()` with `updated_at=None` would silently pick up the
    column default and land the row with the current time, which is why the
    column carries no default at all. This test is the guard: if someone
    reinstates `default=utcnow`, the NULL disappears and the startup refresh
    stops recognising untouched copies.
    """
    async with factory() as session:
        user_id = uuid.uuid4()
        session.add(
            sa.inspect(Prompt).class_(
                user_id=user_id,
                name="Summary",
                system_prompt="vendor text",
                is_system=True,
                created_at=datetime.now(),
                updated_at=None,
            )
        )
        await session.commit()

        row = (
            await session.scalars(sa.select(Prompt).where(Prompt.user_id == user_id))
        ).one()
        assert row.is_system is True
        assert row.updated_at is None, "a fresh system copy must carry no edit timestamp"
```

Note on the `factory` fixture: it is **not** in `conftest.py` — each DB test file
defines its own. Copy the one from `tests/test_worker_pool.py:90-97` verbatim
(it builds an engine, drops and recreates the schema, and yields an
`async_sessionmaker`), along with the imports it needs.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: FAIL — `Prompt` has no attribute `is_system`.

- [ ] **Step 3: Change the model**

In `vts/db/models.py`, inside `class Prompt`, replace the `updated_at` line and add
`is_system`:

```python
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # True for a user's copy of a vendor prompt, False for one they wrote.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Answers one question: when did the *user* change this? NULL means never,
    # which is how the startup refresh tells an untouched vendor copy from an
    # edited one. Deliberately carries no default and no onupdate — a default
    # would override the NULL a fresh system copy needs, and the workaround
    # (a Core insert) breaks silently the moment someone writes session.add()
    # instead. Every write site sets it explicitly.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`Boolean`, `Index` and `Text` are already imported at the top of the file — nothing
to add there.

Then extend `__table_args__`:

```python
    __table_args__ = (
        Index("ix_prompts_user_created", "user_id", "created_at"),
        # One vendor copy per user. Without this, the API and the worker
        # creating the copy at the same moment both succeed and the user sees
        # a duplicate.
        Index(
            "ix_prompts_one_system_per_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("is_system"),
        ),
    )
```

Add `import sqlalchemy as sa` at the top of `models.py` if absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: PASS

- [ ] **Step 5: Generate the migration**

```bash
./.venv/bin/alembic revision -m "prompt is_system and nullable updated_at"
```

Fill the generated file's `upgrade()`/`downgrade()`:

```python
def upgrade() -> None:
    op.add_column(
        "prompts",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("prompts", "is_system", server_default=None)
    # Existing rows keep their timestamps: they are user prompts, and their
    # updated_at means what it always meant.
    op.alter_column("prompts", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.create_index(
        "ix_prompts_one_system_per_user",
        "prompts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_system"),
    )


def downgrade() -> None:
    op.drop_index("ix_prompts_one_system_per_user", table_name="prompts")
    op.execute("UPDATE prompts SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column("prompts", "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_column("prompts", "is_system")
```

- [ ] **Step 6: Verify the migration applies**

```bash
sudo podman start vts-test-pg
./.venv/bin/python -m pytest tests/ -q -k "migration or preflight"
```
Expected: PASS. If the repo has a migration-chain test, it verifies `down_revision`
is wired correctly.

- [ ] **Step 7: Commit**

```bash
git add vts/db/models.py alembic/versions tests/test_prompt_system_copy.py vts/__init__.py
git commit -m "feat(db): is_system on prompts, updated_at records user edits

updated_at loses its default and onupdate and becomes nullable: it now
answers when the *user* changed a prompt, and NULL means never. That is
what lets the startup refresh tell an untouched vendor copy from an edited
one. A default would override the NULL a system copy needs, and the Core
insert that works around it breaks silently the moment someone writes
session.add() instead.

Part of vts-kujy."
```

(Bump `__version__` in the same commit per the global constraints.)

---

### Task 2: Explicit `updated_at` at every write site

**Files:**
- Modify: `vts/db/repo.py:638-641` (`create_prompt`), and `update_prompt` in the same file
- Test: `tests/test_prompt_system_copy.py`

**Interfaces:**
- Consumes: `Prompt.updated_at` (Task 1).
- Produces: `Repo.create_prompt(user_id, name, system_prompt, *, is_system: bool = False) -> Prompt` — sets `updated_at=utcnow()` for a user prompt and leaves it `None` for a system copy.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompt_system_copy.py`:

```python
@pytest.mark.asyncio
async def test_create_prompt_stamps_a_user_prompt_and_not_a_system_copy(factory) -> None:
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        user_id = uuid.uuid4()
        mine = await repo.create_prompt(user_id, "Mine", "body")
        vendor = await repo.create_prompt(user_id, "Summary", "vendor", is_system=True)
        await session.commit()

    assert mine.updated_at is not None, "a prompt the user wrote is edited by definition"
    assert mine.is_system is False
    assert vendor.updated_at is None, "a vendor copy has not been touched by the user"
    assert vendor.is_system is True


@pytest.mark.asyncio
async def test_update_prompt_stamps_updated_at(factory) -> None:
    """Editing is what puts a timestamp on a system copy."""
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        user_id = uuid.uuid4()
        vendor = await repo.create_prompt(user_id, "Summary", "vendor", is_system=True)
        await session.commit()
        assert vendor.updated_at is None

        await repo.update_prompt(
            user_id, vendor.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    assert vendor.updated_at is not None, "an edit must be recorded"
    assert vendor.system_prompt == "my own wording"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: FAIL — `create_prompt()` got an unexpected keyword argument `is_system`.

- [ ] **Step 3: Set the timestamps explicitly**

In `vts/db/repo.py`, replace `create_prompt`:

```python
    async def create_prompt(
        self,
        user_id: uuid.UUID,
        name: str,
        system_prompt: str,
        *,
        is_system: bool = False,
    ) -> Prompt:
        prompt = Prompt(
            user_id=user_id,
            name=name,
            system_prompt=system_prompt,
            is_system=is_system,
            created_at=utcnow(),
            # A prompt the user wrote is edited by definition; a vendor copy
            # has not been touched yet, and NULL is what says so.
            updated_at=None if is_system else utcnow(),
        )
        self.session.add(prompt)
        await self.session.flush()
        return prompt
```

In `update_prompt`, after the two `if ... is not None:` assignments and before the
flush, add:

```python
        prompt.updated_at = utcnow()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite for fallout**

```bash
./.venv/bin/python -m pytest tests/ -q --ignore=tests/ui
```
Expected: PASS. Existing tests construct `Prompt` directly in about 16 places; any
that assert on `updated_at` being set will now need the value passed explicitly —
fix those by passing it, not by reinstating the column default.

- [ ] **Step 6: Commit**

```bash
git add vts/db/repo.py tests/test_prompt_system_copy.py
git commit -m "feat(db): set prompt timestamps explicitly

Part of vts-kujy."
```

---

### Task 3: Lazy creation of the user's copy

**Files:**
- Create: `vts/services/system_prompt.py`
- Test: `tests/test_prompt_system_copy.py`

**Interfaces:**
- Consumes: `Repo.create_prompt(..., is_system=True)` (Task 2).
- Produces: `async get_or_create_system_prompt(session, user_id: UUID, prompts_dir: Path) -> Prompt`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompt_system_copy.py`:

```python
@pytest.mark.asyncio
async def test_get_or_create_reads_the_file_once_then_reuses_the_row(
    factory, tmp_path
) -> None:
    """The copy is made on first use, not at signup.

    A task runs in the worker, so a user who has never opened the UI still has
    no row when their first summary starts. Creating on demand also means a row
    lost for any reason simply comes back.
    """
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        first = await get_or_create_system_prompt(session, user_id, tmp_path)
        await session.commit()
        first_id = first.id

    assert first.system_prompt == "vendor text"
    assert first.is_system is True
    assert first.updated_at is None

    # A second call must not make a second copy.
    async with factory() as session:
        again = await get_or_create_system_prompt(session, user_id, tmp_path)
        await session.commit()
    assert again.id == first_id

    async with factory() as session:
        rows = (
            await session.scalars(sa.select(Prompt).where(Prompt.user_id == user_id))
        ).all()
    assert len(rows) == 1, "the vendor copy must exist exactly once per user"


@pytest.mark.asyncio
async def test_get_or_create_returns_an_edited_copy_unchanged(factory, tmp_path) -> None:
    """Once the user has edited it, the file is no longer consulted."""
    from vts.db.repo import Repo
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        row = await get_or_create_system_prompt(session, user_id, tmp_path)
        await Repo(session).update_prompt(
            user_id, row.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    (tmp_path / "global_prompt.md").write_text("a newer vendor text", encoding="utf-8")

    async with factory() as session:
        again = await get_or_create_system_prompt(session, user_id, tmp_path)
    assert again.system_prompt == "my own wording"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: FAIL — no module `vts.services.system_prompt`.

- [ ] **Step 3: Write the service**

Create `vts/services/system_prompt.py`:

```python
"""Per-user copies of the vendor system prompt (vts-kujy).

The vendor's text stays in `prompts/global_prompt.md`; the database holds each
user's copy of it. Keeping the file as the only reference is what makes
restoring free — deleting the row and reading the file again is the whole
mechanism — at the cost that a copy, once made, does not follow later edits to
the file on its own. `refresh_untouched_system_prompts` closes that gap.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import Prompt
from vts.db.repo import Repo
from vts.services.prompt_registry import list_system_prompts
from vts.services.summarizer import load_prompt

_SUMMARY_KEY = "summary"
_FALLBACK = "Produce a structured knowledge document from the notes."


def vendor_text(prompts_dir: Path) -> str:
    """The vendor's own wording, straight from the file."""
    spec = next((p for p in list_system_prompts() if p.key == _SUMMARY_KEY), None)
    file = spec.file if spec is not None else "global_prompt.md"
    return load_prompt(prompts_dir, file, _FALLBACK)


def vendor_name(default: str = "Summary") -> str:
    spec = next((p for p in list_system_prompts() if p.key == _SUMMARY_KEY), None)
    return spec.display_name if spec is not None else default


async def get_or_create_system_prompt(
    session: AsyncSession, user_id: uuid.UUID, prompts_dir: Path
) -> Prompt:
    """The user's copy of the vendor prompt, made from the file if absent.

    Called from the API when the user opens the prompt list, and from the
    pipeline when a task resolves `{"source": "system", "id": "summary"}` — a
    user who has never opened the UI still has no copy when their first
    summary runs.
    """
    stmt = sa.select(Prompt).where(Prompt.user_id == user_id, Prompt.is_system)
    existing = (await session.scalars(stmt)).first()
    if existing is not None:
        return existing

    try:
        created = await Repo(session).create_prompt(
            user_id, vendor_name(), vendor_text(prompts_dir), is_system=True
        )
        await session.flush()
        return created
    except IntegrityError:
        # The partial unique index rejected us: another caller created the copy
        # between our SELECT and INSERT. That is the race resolving correctly —
        # re-read and use theirs.
        await session.rollback()
        return (await session.scalars(stmt)).one()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add vts/services/system_prompt.py tests/test_prompt_system_copy.py
git commit -m "feat(prompts): create the user's vendor copy on first use

Part of vts-kujy."
```

---

### Task 4: Resolve the pipeline and API through the copy

**Files:**
- Modify: `vts/pipeline/steps/summarization.py:450-461` (`load_text`), `:1029-1036` (the `pack_window_notes` read)
- Modify: `vts/api/routers/meta.py:164-178` (list), `:192-205` (system text)
- Modify: `vts/api/schemas.py:22-27` (`PromptOut`)
- Test: `tests/test_prompt_system_copy.py`

**Interfaces:**
- Consumes: `get_or_create_system_prompt` (Task 3).
- Produces: `PromptOut.is_system: bool` — the frontend's only way to tell the two cases apart.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompt_system_copy.py`:

```python
@pytest.mark.asyncio
async def test_pipeline_uses_the_users_edited_copy(factory, tmp_path) -> None:
    """The summary must run on what the user wrote, not on the file."""
    from vts.db.repo import Repo
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        row = await get_or_create_system_prompt(session, user_id, tmp_path)
        await Repo(session).update_prompt(
            user_id, row.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    async with factory() as session:
        resolved = await get_or_create_system_prompt(session, user_id, tmp_path)
    assert resolved.system_prompt == "my own wording"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py::test_pipeline_uses_the_users_edited_copy -v`
Expected: PASS already — Task 3 covers the service. The real change here is the two
call sites, which the suite exercises indirectly; treat Step 5 (full suite) as this
task's gate.

- [ ] **Step 3: Route the pipeline through the copy**

In `vts/pipeline/steps/summarization.py`, replace the body of `load_text` (around
line 450) with:

```python
    async def load_text(self, ctx, id, output_language, user_id) -> str:
        sysdef = next((p for p in list_system_prompts() if p.key == id), None)
        if sysdef is None:
            raise RuntimeError(f"unknown system prompt: {id}")
        # The user's copy, not the file: they may have edited it, and the copy
        # is created here if this is their first summary (vts-kujy).
        from vts.services.system_prompt import get_or_create_system_prompt

        async with ctx.session_factory() as session:
            prompt = await get_or_create_system_prompt(
                session, uuid.UUID(str(user_id)), ctx.settings.prompts_dir
            )
            text = prompt.system_prompt
            await session.commit()
        return render_prompt_with_language(text, output_language)
```

At the `pack_window_notes` read (around line 1029), the prompt is loaded only to
measure its token cost. Replace the `load_prompt(...)` call with the same lookup:

```python
        from vts.services.system_prompt import get_or_create_system_prompt

        async with ctx.session_factory() as session:
            _sys_prompt = await get_or_create_system_prompt(
                session, uuid.UUID(str(st.user_id)), ctx.settings.prompts_dir
            )
            _sys_text = _sys_prompt.system_prompt
            await session.commit()
        final_prompt_text = render_prompt_vars(
            render_prompt_with_language(_sys_text, output_language),
        )
```

Add `import uuid` at the top of the file if absent.

- [ ] **Step 4: Expose `is_system` and serve the copy from the API**

In `vts/api/schemas.py`, add the field to `PromptOut`:

```python
class PromptOut(BaseModel):
    source: str
    id: str
    name: str
    editable: bool
    # True for the user's copy of a vendor prompt: the editor offers "Restore"
    # instead of "Delete" for it.
    is_system: bool = False
```

In `vts/api/routers/meta.py`, replace the list endpoint body:

```python
    repo = Repo(session)
    out: list[PromptOut] = []
    for row in await repo.list_prompts(uuid.UUID(user.id)):
        out.append(
            PromptOut(
                source="user",
                id=str(row.id),
                name=row.name,
                editable=True,
                is_system=row.is_system,
            )
        )
    return out
```

The system prompt is no longer listed separately — it is one of the user's rows now.

Replace the body of `get_system_prompt_text_endpoint` so it returns the user's copy:

```python
    from vts.services.system_prompt import get_or_create_system_prompt

    if key != "summary":
        raise HTTPException(status_code=404, detail="System prompt not found")
    prompt = await get_or_create_system_prompt(
        session, uuid.UUID(user.id), settings.prompts_dir
    )
    await session.commit()
    return SystemPromptTextOut(system_prompt=prompt.system_prompt)
```

Add `session: AsyncSession = Depends(get_session_dep)` to that endpoint's signature.

- [ ] **Step 5: Run the whole suite**

```bash
sudo podman start vts-test-pg
./.venv/bin/python -m pytest tests/ -q --ignore=tests/ui
```
Expected: PASS. Tests asserting the old shape of `GET /api/prompts` (a system entry
with `editable=False`) will need updating to the new shape — the system prompt is
now a user row with `is_system=True, editable=True`.

- [ ] **Step 6: Commit**

```bash
git add vts/pipeline/steps/summarization.py vts/api/routers/meta.py vts/api/schemas.py tests/test_prompt_system_copy.py
git commit -m "feat(prompts): resolve the system prompt through the user's copy

Part of vts-kujy."
```

---

### Task 5: Refresh untouched copies on startup

**Files:**
- Modify: `vts/services/system_prompt.py`
- Modify: `docker/vts-entrypoint.sh` (the `migrate` function)
- Test: `tests/test_prompt_system_copy.py`

**Interfaces:**
- Consumes: `vendor_text` (Task 3).
- Produces: `async refresh_untouched_system_prompts(session, prompts_dir: Path) -> tuple[int, int]` — returns `(refreshed, skipped)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompt_system_copy.py`:

```python
@pytest.mark.asyncio
async def test_refresh_rewrites_untouched_copies_and_spares_edited_ones(
    factory, tmp_path
) -> None:
    """A newly shipped prompt has to reach users who already have a copy.

    Inferring "untouched" by comparing the copy to the current file does not
    work: a copy made from v1 and never touched does not match v2 either, so
    every untouched copy would read as edited exactly when the refresh is
    needed. The record — `updated_at IS NULL` — survives any number of
    releases, which this test pins by shipping *two* new versions.
    """
    from vts.db.repo import Repo
    from vts.services.system_prompt import (
        get_or_create_system_prompt,
        refresh_untouched_system_prompts,
    )

    (tmp_path / "global_prompt.md").write_text("v1", encoding="utf-8")
    untouched_user = uuid.uuid4()
    editing_user = uuid.uuid4()

    async with factory() as session:
        await get_or_create_system_prompt(session, untouched_user, tmp_path)
        edited = await get_or_create_system_prompt(session, editing_user, tmp_path)
        await Repo(session).update_prompt(
            editing_user, edited.id, name=None, system_prompt="mine"
        )
        await session.commit()

    (tmp_path / "global_prompt.md").write_text("v2", encoding="utf-8")
    async with factory() as session:
        refreshed, skipped = await refresh_untouched_system_prompts(session, tmp_path)
        await session.commit()
    assert (refreshed, skipped) == (1, 1)

    # Second release: the untouched copy now holds v2, which matches neither
    # v1 nor v3 — comparing text would call it edited from here on.
    (tmp_path / "global_prompt.md").write_text("v3", encoding="utf-8")
    async with factory() as session:
        refreshed, skipped = await refresh_untouched_system_prompts(session, tmp_path)
        await session.commit()
    assert (refreshed, skipped) == (1, 1)

    async with factory() as session:
        rows = {
            r.user_id: r
            for r in (await session.scalars(sa.select(Prompt).where(Prompt.is_system))).all()
        }
    assert rows[untouched_user].system_prompt == "v3"
    assert rows[untouched_user].updated_at is None, "a refresh is not a user edit"
    assert rows[editing_user].system_prompt == "mine"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -k refresh -v`
Expected: FAIL — cannot import `refresh_untouched_system_prompts`.

- [ ] **Step 3: Write the refresh**

Append to `vts/services/system_prompt.py`:

```python
async def refresh_untouched_system_prompts(
    session: AsyncSession, prompts_dir: Path
) -> tuple[int, int]:
    """Rewrite every vendor copy the user has not edited. Returns (refreshed, skipped).

    `updated_at IS NULL` is the whole test: it records that the user has never
    edited this copy, and it keeps meaning that across any number of releases.
    The refresh does **not** stamp `updated_at` — rewriting a copy is not a
    user edit, and stamping it would make the copy immune to the next release.
    """
    text = vendor_text(prompts_dir)

    skipped = len(
        (
            await session.scalars(
                sa.select(Prompt.id).where(Prompt.is_system, Prompt.updated_at.is_not(None))
            )
        ).all()
    )
    result = await session.execute(
        sa.update(Prompt)
        .where(Prompt.is_system, Prompt.updated_at.is_(None))
        .values(system_prompt=text)
    )
    refreshed = int(result.rowcount or 0)
    logger.info(
        "system prompt refresh: %d untouched copies rewritten, %d left as edited",
        refreshed,
        skipped,
    )
    return refreshed, skipped
```

Add at the top of the module:

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_prompt_system_copy.py -v`
Expected: PASS

- [ ] **Step 5: Add the CLI entry point**

There is no `vts/cli/` package yet — create the directory with an empty
`__init__.py`, then `vts/cli/refresh_system_prompts.py`:

```python
"""Rewrite untouched vendor prompt copies. Run once per deploy (vts-kujy)."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vts.core.config import get_settings
from vts.services.system_prompt import refresh_untouched_system_prompts


async def _main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await refresh_untouched_system_prompts(session, settings.prompts_dir)
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 6: Wire it into the migrate step**

In `docker/vts-entrypoint.sh`, inside the `migrate()` function, after the
`alembic upgrade head` line:

```sh
  # Untouched copies of the vendor system prompt pick up a newly shipped
  # version here: this runs once per deploy, before webapi or worker starts
  # serving, so there is no race on the mass UPDATE and no window where the
  # worker creates a copy from the old file afterwards (vts-kujy).
  python -m vts.cli.refresh_system_prompts
```

- [ ] **Step 7: Run the whole suite**

```bash
./.venv/bin/python -m pytest tests/ -q --ignore=tests/ui
```
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add vts/services/system_prompt.py vts/cli/__init__.py vts/cli/refresh_system_prompts.py docker/vts-entrypoint.sh tests/test_prompt_system_copy.py
git commit -m "feat(prompts): refresh untouched vendor copies on deploy

Part of vts-kujy."
```

---

### Task 6: Restore button and delete confirmation

**Files:**
- Modify: `vts/static/app.js:5399-5409` (the delete handler), `:5509` (`syncPromptEditorState`)
- Modify: `vts/static/i18n/en.js`, `vts/static/i18n/ru.js`, `vts/static/i18n/de.js`
- Create: `tests/ui/scenarios/system-prompt-restore.mjs`

**Interfaces:**
- Consumes: `PromptOut.is_system` (Task 4).
- Produces: nothing.

- [ ] **Step 1: Add the strings**

In each of `vts/static/i18n/{en,ru,de}.js`, next to the existing prompt keys:

```javascript
// en.js
"action.prompt_restore": "Restore",
"action.prompt_restore_tooltip": "Put the vendor's version back",
"confirm.prompt_restore": "Restore the vendor's version? Your edits to this prompt will be lost.",
"confirm.prompt_delete": "Delete this prompt? This cannot be undone.",

// ru.js
"action.prompt_restore": "Восстановить",
"action.prompt_restore_tooltip": "Вернуть вендорскую версию промпта",
"confirm.prompt_restore": "Вернуть вендорскую версию? Ваши правки этого промпта будут потеряны.",
"confirm.prompt_delete": "Удалить промпт? Это действие необратимо.",

// de.js
"action.prompt_restore": "Zurücksetzen",
"action.prompt_restore_tooltip": "Die Version des Anbieters wiederherstellen",
"confirm.prompt_restore": "Die Version des Anbieters wiederherstellen? Ihre Änderungen an diesem Prompt gehen verloren.",
"confirm.prompt_delete": "Prompt löschen? Dies kann nicht rückgängig gemacht werden.",
```

- [ ] **Step 2: Make the button do both jobs**

In `vts/static/app.js`, replace the delete handler (around line 5399):

```javascript
document.getElementById("prompt-delete-btn")?.addEventListener("click", async () => {
  const id = promptEditIdInput?.value;
  if (!id) return;
  // The same button restores a system prompt and deletes a user one: for the
  // system prompt, deleting the row *is* the restore, because the next request
  // recreates it from the vendor file.
  const isSystem = Boolean(currentPromptIsSystem);
  const confirmed = window.confirm(
    t(isSystem ? "confirm.prompt_restore" : "confirm.prompt_delete")
  );
  if (!confirmed) return;
  const resp = await fetch(buildPath(`/api/prompts/${encodeURIComponent(id)}`), { method: "DELETE" });
  if (!resp.ok) return;
  await refreshPromptsManager();
  await loadPrompts();
  if (isSystem) {
    // Re-open it so the restored text is on screen without a manual reload.
    const restored = await fetch(buildPath("/api/prompts/system/summary/text"));
    if (restored.ok) {
      const data = await restored.json();
      const body = document.getElementById("prompt-body-input");
      if (body) body.value = data.system_prompt || "";
      updatePromptBodyMeta();
      return;
    }
  }
  resetPromptForm();
  syncPromptEditorState(null);
});
```

Declare the flag near the other prompt-editor module state (next to
`promptEditIdInput`):

```javascript
let currentPromptIsSystem = false;
```

and set it in `syncPromptEditorState` (around line 5509), where the button's
visibility is already decided:

```javascript
function syncPromptEditorState(prompt) {
  const editable = Boolean(prompt?.editable);
  currentPromptIsSystem = Boolean(prompt?.is_system);
  const bodyInput = document.getElementById("prompt-body-input");
  const nameInput = document.getElementById("prompt-name-input");
  if (bodyInput) bodyInput.readOnly = prompt ? !editable : false;
  if (nameInput) nameInput.readOnly = prompt ? !editable : false;
  const deleteBtn = document.getElementById("prompt-delete-btn");
  deleteBtn?.classList.toggle("hidden", !editable);
  if (deleteBtn) {
    // Same button, same place — only the wording changes with the context.
    const labelKey = currentPromptIsSystem ? "action.prompt_restore" : "action.prompt_delete";
    const tipKey = currentPromptIsSystem
      ? "action.prompt_restore_tooltip"
      : "action.prompt_delete_tooltip";
    const label = deleteBtn.querySelector("span") || deleteBtn;
    label.textContent = t(labelKey);
    deleteBtn.setAttribute("data-tooltip", t(tipKey));
    deleteBtn.setAttribute("aria-label", t(labelKey));
  }
  document.getElementById("prompt-duplicate-btn")?.classList.toggle("hidden", !prompt);
  const submit = document.getElementById("prompt-submit-btn");
  if (submit) submit.classList.toggle("hidden", Boolean(prompt) && !editable);
  updatePromptBodyMeta();
}
```

If `action.prompt_delete` / `action.prompt_delete_tooltip` do not already exist in
the locales, add them with the current button wording.

Note the known trap: setting `textContent` on a button that holds an SVG icon
deletes the icon. Put the label in an inner `<span>` — the code above targets
`deleteBtn.querySelector("span")` first for that reason. Check
`vts/static/index.html` for the button's markup and add a `<span>` around its
label if there is none.

- [ ] **Step 3: Write the UI scenario**

Create `tests/ui/scenarios/system-prompt-restore.mjs` following the shape of an
existing scenario (`tests/ui/scenarios/tooltip-icon-buttons.mjs` is a short one):
stub `/api/prompts` to return one row with `is_system: true`, open the prompt
editor, and assert that

1. the button reads the restore label, not the delete label;
2. dismissing the confirm sends no `DELETE`;
3. accepting it sends `DELETE` and then `GET /api/prompts/system/summary/text`.

- [ ] **Step 4: Run the UI suite**

```bash
cd tests/ui && node run.mjs
```
Expected: `UI VERIFY: PASSED`

- [ ] **Step 5: Regenerate the UI inventory**

```bash
make ui-inventory
```

- [ ] **Step 6: Commit**

```bash
git add vts/static/app.js vts/static/i18n docs/ui-inventory.md tests/ui/scenarios/system-prompt-restore.mjs vts/static/index.html
git commit -m "feat(ui): restore the vendor prompt with the delete button

The same button in the same place reads Restore for a system prompt,
because deleting that row is what brings the vendor text back. Both actions
now confirm first — deleting a user prompt was irreversible and silent.

Part of vts-kujy."
```

---

## Self-Review

**Spec coverage:**

| spec section | task |
|---|---|
| `is_system` boolean, partial unique index | 1 |
| `updated_at` explicit, nullable, no default | 1 (column), 2 (write sites) |
| Alembic migration | 1 |
| Lazy creation, `IntegrityError` retry | 3 |
| Called from API and pipeline | 4 |
| `is_system` in `PromptOut` | 4 |
| Restore is `DELETE`, unchanged backend | 6 (frontend only — no backend step exists, by design) |
| Refresh untouched copies, once per deploy | 5 |
| Refresh logs counts | 5 |
| Confirmation for both actions | 6 |
| i18n in three locales | 6 |
| Test list from the spec | 1-6, all present |

**Placeholder scan:** no TBD/TODO. Two steps name a file to imitate rather than
quoting it — Task 1's `factory` fixture and Task 6's UI scenario — because both
depend on repo conventions the plan should not guess at; each names the exact file
to copy from.

**Type consistency:** `get_or_create_system_prompt(session, user_id, prompts_dir)`
is called with those three arguments in Tasks 4 and 5; `create_prompt`'s new
keyword is `is_system` in both Task 2 and Task 3; `refresh_untouched_system_prompts`
returns `(refreshed, skipped)` and Task 5's test unpacks exactly two values.

**Ordering note:** Task 4 removes the separate system entry from `GET /api/prompts`,
so between Tasks 4 and 6 the frontend will show the system prompt as an ordinary
editable row with a Delete button that in fact restores. That window is inside one
plan and closes at Task 6; it is called out here so a reviewer does not read it as
a defect at Task 4.
