# Delivery Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give VTS a universal, pluggable mechanism to deliver a task's result (raw / redacted / summary transcript) to external systems after completion, with Outline as the first adapter — without the VTS core knowing about any specific external system.

**Architecture:** The core defines a `DeliveryAdapter` Protocol discovered via Python `entry_points("vts.delivery")`; adapters live in separate pip packages. Per-user `DeliveryTarget` rows hold adapter name + non-secret config + encrypted secrets. On task completion the processor enqueues one durable `DeliveryAttempt` row per configured destination; a consumer loop in the worker claims rows (`FOR UPDATE SKIP LOCKED`), resolves the variant's content, decrypts secrets, calls the adapter, and records success/backoff/dead. Delivery is a post-completion side effect — it can never fail the transcription.

**Tech Stack:** Python 3.14, SQLAlchemy 2 async, Alembic, FastAPI, FastMCP, Redis (pub/sub wake-up), `cryptography` (Fernet) for at-rest secret encryption, pytest.

## Global Constraints

- **Version bump:** bump the version in `vts/__init__.py` before committing (project rule).
- **JSON columns:** `Task.options` and any JSON column must be **reassigned, not mutated in place** (SQLAlchemy does not detect in-place dict mutation). Build a new dict and assign.
- **Secrets are write-only:** decrypted secret values must NEVER appear in any REST response, MCP tool result, log line, or SSE event. Only `{key: {"set": bool}}` presence markers may be exposed.
- **Source of truth is the DB row**, not the Redis message. Redis `delivery:notify` is only a wake-up; correctness must not depend on it arriving.
- **Delivery never fails the task:** the enqueue call site is wrapped so any exception is logged and the task stays `completed`.
- **Migrations** are numbered `NNNN_name.py` with `revision`/`down_revision` string ids chained to the previous head. Current head: `0019_match_decision_is_noise`.
- **`cryptography` is a new dependency** — add it to `pyproject.toml` and lock it in Task 1.
- **Ядро не импортирует ни один адаптер** — no `import vts_outline` anywhere in `vts/`. The Outline package depends on `vts`, never the reverse.

---

## File Structure

**Core (in the `vts` repo):**
- `vts/core/secrets.py` — Fernet encrypt/decrypt of a secrets dict; key from `VTS_SECRETS_KEY`.
- `vts/delivery/contract.py` — `DeliveryAdapter` Protocol, `DeliveryPayload`, `DeliveryResult`, `TaskMeta`, `DeliveryTargetConfig`, `DeliveryError`.
- `vts/delivery/registry.py` — discovery via `entry_points("vts.delivery")`, `get_adapter(name)`, `list_adapters()`.
- `vts/delivery/resolve.py` — `resolve_variant(task, variant) -> DeliveryPayload` (reads content from `artifact_dir`).
- `vts/delivery/queue.py` — `enqueue_deliveries(session, repo, task)`; backoff helper.
- `vts/delivery/consumer.py` — `delivery_loop()` (claim / deliver / record) + `reap_stuck_deliveries`.
- `vts/db/models.py` — `+DeliveryStatus`, `+DeliveryTarget`, `+DeliveryAttempt`.
- `vts/db/repo.py` — target CRUD + attempt claim/record/list.
- `vts/api/schemas.py`, `vts/api/main.py` — REST endpoints for targets + task deliveries.
- `vts/mcp/schemas.py`, `vts/mcp/tools.py`, `vts/mcp/server.py` — MCP CRUD + status + retry; `delivery` on submit.
- `vts/services/preset_expand.py` — add `delivery` to the options allowlist.
- `vts/core/config.py` — new `Settings` fields.
- `alembic/versions/0020_*.py`, `0021_*.py` — two migrations.

**Plugin (separate package `vts-outline/`):**
- `vts-outline/pyproject.toml` — declares `[project.entry-points."vts.delivery"] outline = ...`.
- `vts-outline/vts_outline/__init__.py` — `OutlineAdapter`.
- `vts-outline/tests/` — adapter tests against a mocked Outline API.

---

## Task 1: Add `cryptography` dependency + secret encryption helper

**Files:**
- Modify: `pyproject.toml` (dependencies), `vts/__init__.py` (version bump)
- Create: `vts/core/secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces:
  - `encrypt_secrets(data: dict[str, str], key: str) -> bytes`
  - `decrypt_secrets(blob: bytes, key: str) -> dict[str, str]`
  - `class SecretsKeyMissing(RuntimeError)`
  - `load_secrets_key(settings) -> str` (raises `SecretsKeyMissing` if unset/blank)

- [ ] **Step 1: Add dependency and lock**

Add to `pyproject.toml` `[project].dependencies`: `"cryptography>=43,<46"`. Then run:

```bash
uv add "cryptography>=43,<46"
```
(If `uv add` edits pyproject itself, do not double-add — just ensure the entry exists and `uv.lock` updates.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_secrets.py
import pytest
from cryptography.fernet import Fernet
from vts.core.secrets import encrypt_secrets, decrypt_secrets, SecretsKeyMissing, load_secrets_key


def test_encrypt_decrypt_roundtrip():
    key = Fernet.generate_key().decode()
    data = {"api_token": "s3cr3t", "other": "value"}
    blob = encrypt_secrets(data, key)
    assert isinstance(blob, (bytes, bytearray))
    assert b"s3cr3t" not in blob  # ciphertext, not plaintext
    assert decrypt_secrets(blob, key) == data


def test_empty_dict_roundtrip():
    key = Fernet.generate_key().decode()
    assert decrypt_secrets(encrypt_secrets({}, key), key) == {}


def test_load_secrets_key_missing_raises():
    class S:
        secrets_key = ""
    with pytest.raises(SecretsKeyMissing):
        load_secrets_key(S())
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_secrets.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.core.secrets`.

- [ ] **Step 4: Write minimal implementation**

```python
# vts/core/secrets.py
from __future__ import annotations

import json

from cryptography.fernet import Fernet


class SecretsKeyMissing(RuntimeError):
    """VTS_SECRETS_KEY is not configured but a secret operation was requested."""


def load_secrets_key(settings) -> str:
    key = getattr(settings, "secrets_key", "") or ""
    if not key.strip():
        raise SecretsKeyMissing(
            "VTS_SECRETS_KEY is not set; delivery targets with secrets are unavailable"
        )
    return key


def encrypt_secrets(data: dict[str, str], key: str) -> bytes:
    payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
    return Fernet(key.encode("utf-8")).encrypt(payload)


def decrypt_secrets(blob: bytes, key: str) -> dict[str, str]:
    raw = Fernet(key.encode("utf-8")).decrypt(bytes(blob))
    return json.loads(raw.decode("utf-8"))
```

- [ ] **Step 5: Add the `secrets_key` setting**

In `vts/core/config.py` `class Settings`, add near other secret-ish fields:

```python
    secrets_key: str = ""  # VTS_SECRETS_KEY: Fernet key for delivery-target secrets
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_secrets.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Bump version and commit**

Bump `vts/__init__.py` version.

```bash
git add pyproject.toml uv.lock vts/core/secrets.py vts/core/config.py tests/test_secrets.py vts/__init__.py
git commit -m "feat(secrets): Fernet at-rest encryption helper for delivery secrets (vts-ouq)"
```

---

## Task 2: Delivery contract types

**Files:**
- Create: `vts/delivery/__init__.py` (empty), `vts/delivery/contract.py`
- Test: `tests/delivery/__init__.py` (empty), `tests/delivery/test_contract.py`

**Interfaces:**
- Produces (all `from vts.delivery.contract import ...`):
  - `@dataclass(frozen=True) class TaskMeta` — fields `source_url: str`, `source_title: str | None`, `language: str | None`, `duration_s: float | None`, `created_at: datetime`
  - `@dataclass(frozen=True) class DeliveryPayload` — `task_id: str`, `variant: str`, `content: str`, `content_format: str`, `task: TaskMeta`
  - `@dataclass(frozen=True) class DeliveryTargetConfig` — `config: dict[str, Any]`, `secrets: dict[str, str]`
  - `@dataclass(frozen=True) class DeliveryResult` — `external_id: str | None = None`, `external_url: str | None = None`
  - `class DeliveryError(Exception)` — adapters raise this (or any Exception) to signal retryable failure
  - `class DeliveryAdapter(Protocol)` — `name: str`; `config_schema(self) -> dict`; `secret_keys(self) -> list[str]`; `async def deliver(self, payload: DeliveryPayload, target: DeliveryTargetConfig) -> DeliveryResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/delivery/test_contract.py
from datetime import datetime, timezone
from vts.delivery.contract import (
    TaskMeta, DeliveryPayload, DeliveryTargetConfig, DeliveryResult, DeliveryError,
)


def test_payload_is_frozen():
    meta = TaskMeta(source_url="u", source_title="t", language="en",
                    duration_s=1.0, created_at=datetime.now(timezone.utc))
    p = DeliveryPayload(task_id="x", variant="summary", content="c",
                        content_format="markdown", task=meta)
    assert p.variant == "summary"
    assert p.task.source_url == "u"


def test_result_defaults_none():
    r = DeliveryResult()
    assert r.external_id is None and r.external_url is None


def test_delivery_error_is_exception():
    assert issubclass(DeliveryError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/delivery/test_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.delivery.contract`.

- [ ] **Step 3: Write minimal implementation**

```python
# vts/delivery/contract.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class DeliveryError(Exception):
    """Raised by an adapter to signal a retryable delivery failure."""


@dataclass(frozen=True)
class TaskMeta:
    source_url: str
    source_title: str | None
    language: str | None
    duration_s: float | None
    created_at: datetime


@dataclass(frozen=True)
class DeliveryPayload:
    task_id: str
    variant: str
    content: str
    content_format: str
    task: TaskMeta


@dataclass(frozen=True)
class DeliveryTargetConfig:
    config: dict[str, Any]
    secrets: dict[str, str]


@dataclass(frozen=True)
class DeliveryResult:
    external_id: str | None = None
    external_url: str | None = None


@runtime_checkable
class DeliveryAdapter(Protocol):
    name: str

    def config_schema(self) -> dict: ...
    def secret_keys(self) -> list[str]: ...
    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult: ...
```

Also create empty `vts/delivery/__init__.py` and `tests/delivery/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/delivery/test_contract.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vts/delivery/__init__.py vts/delivery/contract.py tests/delivery/
git commit -m "feat(delivery): contract types (payload, adapter Protocol, result) (vts-ouq)"
```

---

## Task 3: Adapter registry (entry-point discovery)

**Files:**
- Create: `vts/delivery/registry.py`
- Test: `tests/delivery/test_registry.py`

**Interfaces:**
- Consumes: `DeliveryAdapter` from Task 2.
- Produces:
  - `get_adapter(name: str) -> DeliveryAdapter` (raises `UnknownAdapter` if not found)
  - `list_adapters() -> dict[str, DeliveryAdapter]`
  - `class UnknownAdapter(KeyError)`
  - `_load_from_entry_points() -> dict[str, DeliveryAdapter]` (internal; tests monkeypatch it)

Discovery reads `importlib.metadata.entry_points(group="vts.delivery")`; each entry point loads to an adapter class, instantiated once and keyed by its `.name`. Result cached in a module global; `list_adapters()` populates on first call.

- [ ] **Step 1: Write the failing test**

```python
# tests/delivery/test_registry.py
import pytest
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult


class FakeAdapter:
    name = "fake"
    def config_schema(self): return {"type": "object"}
    def secret_keys(self): return ["token"]
    async def deliver(self, payload, target): return DeliveryResult()


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", None, raising=False)
    monkeypatch.setattr(registry, "_load_from_entry_points",
                        lambda: {"fake": FakeAdapter()})


def test_list_adapters_returns_registered():
    assert set(registry.list_adapters()) == {"fake"}


def test_get_adapter_found():
    assert registry.get_adapter("fake").name == "fake"


def test_get_adapter_unknown_raises():
    with pytest.raises(registry.UnknownAdapter):
        registry.get_adapter("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/delivery/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.delivery.registry`.

- [ ] **Step 3: Write minimal implementation**

```python
# vts/delivery/registry.py
from __future__ import annotations

from importlib.metadata import entry_points

from vts.delivery.contract import DeliveryAdapter

_CACHE: dict[str, DeliveryAdapter] | None = None


class UnknownAdapter(KeyError):
    """No delivery adapter registered under this name."""


def _load_from_entry_points() -> dict[str, DeliveryAdapter]:
    out: dict[str, DeliveryAdapter] = {}
    for ep in entry_points(group="vts.delivery"):
        adapter_cls = ep.load()
        adapter = adapter_cls()
        out[adapter.name] = adapter
    return out


def list_adapters() -> dict[str, DeliveryAdapter]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_from_entry_points()
    return dict(_CACHE)


def get_adapter(name: str) -> DeliveryAdapter:
    adapters = list_adapters()
    try:
        return adapters[name]
    except KeyError as exc:
        raise UnknownAdapter(name) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/delivery/test_registry.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vts/delivery/registry.py tests/delivery/test_registry.py
git commit -m "feat(delivery): entry-point adapter registry (vts-ouq)"
```

---

## Task 4: DB models — DeliveryStatus, DeliveryTarget, DeliveryAttempt + migrations

**Files:**
- Modify: `vts/db/models.py`
- Create: `alembic/versions/0020_delivery_targets.py`, `alembic/versions/0021_delivery_attempts.py`
- Test: `tests/test_delivery_models.py`

**Interfaces:**
- Produces:
  - `class DeliveryStatus(StrEnum)`: `pending`, `delivering`, `delivered`, `failed`, `dead`
  - `class DeliveryTarget(Base)`: `id`, `user_id`, `name`, `adapter`, `config_json`, `secrets_enc`, `created_at`, `updated_at`; unique `(user_id, name)`
  - `class DeliveryAttempt(Base)`: `id`, `task_id`, `target_id`, `adapter`, `variant`, `status`, `attempts`, `max_attempts`, `next_attempt_at`, `last_error`, `external_id`, `external_url`, `created_at`, `updated_at`; index `(status, next_attempt_at)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delivery_models.py
import uuid
import pytest
from vts.db.models import DeliveryStatus, DeliveryTarget, DeliveryAttempt


def test_delivery_status_values():
    assert {s.value for s in DeliveryStatus} == {
        "pending", "delivering", "delivered", "failed", "dead"}


def test_target_columns_exist():
    cols = set(DeliveryTarget.__table__.columns.keys())
    assert {"id", "user_id", "name", "adapter", "config_json",
            "secrets_enc", "created_at", "updated_at"} <= cols


def test_attempt_columns_exist():
    cols = set(DeliveryAttempt.__table__.columns.keys())
    assert {"id", "task_id", "target_id", "adapter", "variant", "status",
            "attempts", "max_attempts", "next_attempt_at", "last_error",
            "external_id", "external_url"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_delivery_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'DeliveryStatus'`.

- [ ] **Step 3: Add the models**

In `vts/db/models.py`, after `StepStatus`, add:

```python
class DeliveryStatus(StrEnum):
    pending = "pending"
    delivering = "delivering"
    delivered = "delivered"
    failed = "failed"
    dead = "dead"
```

After the `Preset` class, add (use `LargeBinary` for `secrets_enc`; import it from sqlalchemy):

```python
class DeliveryTarget(Base):
    __tablename__ = "delivery_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secrets_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_delivery_targets_user_name"),
        Index("ix_delivery_targets_user", "user_id"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_targets.id", ondelete="SET NULL"), nullable=True
    )
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", native_enum=False),
        nullable=False, default=DeliveryStatus.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_delivery_attempts_status_next", "status", "next_attempt_at"),
        Index("ix_delivery_attempts_task", "task_id"),
    )
```

Ensure `LargeBinary` and `UniqueConstraint` are imported at the top of `models.py` (add to the existing sqlalchemy import if missing).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_delivery_models.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write migration 0020 (delivery_targets)**

```python
# alembic/versions/0020_delivery_targets.py
"""delivery_targets

Revision ID: 0020_delivery_targets
Revises: 0019_match_decision_is_noise
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0020_delivery_targets"
down_revision = "0019_match_decision_is_noise"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secrets_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_delivery_targets_user_name"),
    )
    op.create_index("ix_delivery_targets_user", "delivery_targets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_targets_user", table_name="delivery_targets")
    op.drop_table("delivery_targets")
```

- [ ] **Step 6: Write migration 0021 (delivery_attempts)**

```python
# alembic/versions/0021_delivery_attempts.py
"""delivery_attempts

Revision ID: 0021_delivery_attempts
Revises: 0020_delivery_targets
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0021_delivery_attempts"
down_revision = "0020_delivery_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True),
                  sa.ForeignKey("delivery_targets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delivery_attempts_status_next", "delivery_attempts",
                    ["status", "next_attempt_at"])
    op.create_index("ix_delivery_attempts_task", "delivery_attempts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_task", table_name="delivery_attempts")
    op.drop_index("ix_delivery_attempts_status_next", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
```

- [ ] **Step 7: Apply migrations against the test/dev DB and verify head**

Run: `uv run alembic upgrade head && uv run alembic current`
Expected: current revision is `0021_delivery_attempts`. (If the project runs migrations through a script/Makefile target, use that instead — match how CI applies them.)

- [ ] **Step 8: Commit**

```bash
git add vts/db/models.py alembic/versions/0020_delivery_targets.py alembic/versions/0021_delivery_attempts.py tests/test_delivery_models.py
git commit -m "feat(db): delivery_targets + delivery_attempts models & migrations (vts-ouq)"
```

---

## Task 5: DeliveryTarget repo CRUD (encrypted secrets, write-only)

**Files:**
- Modify: `vts/db/repo.py`
- Test: `tests/test_delivery_targets_repo.py`

**Interfaces:**
- Consumes: `encrypt_secrets` / `decrypt_secrets` (Task 1), `DeliveryTarget` (Task 4).
- Produces (methods on `Repo`):
  - `create_delivery_target(user_id, *, name, adapter, config, secrets_enc: bytes | None) -> DeliveryTarget`
  - `list_delivery_targets(user_id) -> list[DeliveryTarget]`
  - `get_delivery_target(user_id, target_id) -> DeliveryTarget | None`
  - `get_delivery_target_by_name(user_id, name) -> DeliveryTarget | None`
  - `update_delivery_target(user_id, target_id, *, name, config, secrets_enc, clear_secrets: bool) -> DeliveryTarget | None` — `secrets_enc=None` + `clear_secrets=False` leaves existing secrets untouched; `clear_secrets=True` sets `secrets_enc=None`.
  - `delete_delivery_target(user_id, target_id) -> bool`

Encryption/decryption happens in the **service/endpoint layer**, not the repo — the repo stores/returns the raw `secrets_enc` blob. This keeps the repo key-agnostic and testable without a key.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delivery_targets_repo.py
import uuid
import pytest
from vts.db.repo import Repo
from vts.db.models import User


async def _user(session):
    u = User(username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_create_and_get_by_name(db_session):
    repo = Repo(db_session)
    u = await _user(db_session)
    t = await repo.create_delivery_target(
        u.id, name="outline-meetings", adapter="outline",
        config={"collection_id": "c1"}, secrets_enc=b"blob")
    await db_session.commit()
    got = await repo.get_delivery_target_by_name(u.id, "outline-meetings")
    assert got is not None and got.id == t.id
    assert got.secrets_enc == b"blob"


@pytest.mark.asyncio
async def test_update_without_secret_keeps_old(db_session):
    repo = Repo(db_session)
    u = await _user(db_session)
    t = await repo.create_delivery_target(
        u.id, name="t", adapter="outline", config={"a": 1}, secrets_enc=b"old")
    await db_session.commit()
    updated = await repo.update_delivery_target(
        u.id, t.id, name=None, config={"a": 2}, secrets_enc=None, clear_secrets=False)
    assert updated.config_json == {"a": 2}
    assert updated.secrets_enc == b"old"  # preserved


@pytest.mark.asyncio
async def test_update_clear_secrets(db_session):
    repo = Repo(db_session)
    u = await _user(db_session)
    t = await repo.create_delivery_target(
        u.id, name="t", adapter="outline", config={}, secrets_enc=b"old")
    await db_session.commit()
    updated = await repo.update_delivery_target(
        u.id, t.id, name=None, config=None, secrets_enc=None, clear_secrets=True)
    assert updated.secrets_enc is None
```

(Use the project's existing async DB session fixture. If the fixture is named differently than `db_session`, match `tests/test_presets_repo.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_delivery_targets_repo.py -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'create_delivery_target'`.

- [ ] **Step 3: Add repo methods**

In `vts/db/repo.py`, after the Preset CRUD block, add (import `DeliveryTarget` at top with the other model imports):

```python
    # ------------------------------------------------------------------
    # DeliveryTarget CRUD
    # ------------------------------------------------------------------

    async def create_delivery_target(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        config: dict, secrets_enc: bytes | None,
    ) -> DeliveryTarget:
        target = DeliveryTarget(
            user_id=user_id, name=name, adapter=adapter,
            config_json=config, secrets_enc=secrets_enc)
        self.session.add(target)
        await self.session.flush()
        return target

    async def list_delivery_targets(self, user_id: uuid.UUID) -> list[DeliveryTarget]:
        stmt = (select(DeliveryTarget)
                .where(DeliveryTarget.user_id == user_id)
                .order_by(DeliveryTarget.created_at.desc()))
        return list(await self.session.scalars(stmt))

    async def get_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> DeliveryTarget | None:
        return await self.session.scalar(
            select(DeliveryTarget).where(
                DeliveryTarget.id == target_id, DeliveryTarget.user_id == user_id))

    async def get_delivery_target_by_name(self, user_id: uuid.UUID, name: str) -> DeliveryTarget | None:
        return await self.session.scalar(
            select(DeliveryTarget).where(
                DeliveryTarget.user_id == user_id, DeliveryTarget.name == name))

    async def update_delivery_target(
        self, user_id: uuid.UUID, target_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        secrets_enc: bytes | None, clear_secrets: bool,
    ) -> DeliveryTarget | None:
        target = await self.get_delivery_target(user_id, target_id)
        if target is None:
            return None
        if name is not None:
            target.name = name
        if config is not None:
            target.config_json = config
        if clear_secrets:
            target.secrets_enc = None
        elif secrets_enc is not None:
            target.secrets_enc = secrets_enc
        await self.session.flush()
        return target

    async def delete_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> bool:
        target = await self.get_delivery_target(user_id, target_id)
        if target is None:
            return False
        await self.session.delete(target)
        await self.session.flush()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_delivery_targets_repo.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_delivery_targets_repo.py
git commit -m "feat(repo): DeliveryTarget CRUD with keep/clear secret semantics (vts-ouq)"
```

---

## Task 6: DeliveryAttempt repo — enqueue, claim, record, list

**Files:**
- Modify: `vts/db/repo.py`
- Test: `tests/test_delivery_attempts_repo.py`

**Interfaces:**
- Consumes: `DeliveryAttempt`, `DeliveryStatus` (Task 4).
- Produces (methods on `Repo`):
  - `create_delivery_attempt(*, task_id, target_id, adapter, variant, max_attempts, next_attempt_at) -> DeliveryAttempt`
  - `claim_due_deliveries(now: datetime, limit: int) -> list[DeliveryAttempt]` — selects `status=pending AND next_attempt_at<=now` ordered by `next_attempt_at`, `LIMIT limit`, `WITH FOR UPDATE SKIP LOCKED`, and flips each to `delivering` (attempts += 1) before returning.
  - `record_delivery_result(attempt_id, *, external_id, external_url) -> None` — sets `delivered`.
  - `record_delivery_failure(attempt_id, *, last_error, next_attempt_at, dead: bool) -> None` — sets `dead` if `dead` else `pending`.
  - `list_deliveries_for_task(task_id) -> list[DeliveryAttempt]`
  - `reap_stuck_deliveries(older_than: datetime) -> int` — flips `delivering` rows whose `updated_at < older_than` back to `pending`.
  - `reset_delivery_for_retry(task_id, target_id: uuid.UUID | None, now) -> int` — flips `failed`/`dead` rows back to `pending` (all for the task if `target_id is None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delivery_attempts_repo.py
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from vts.db.repo import Repo
from vts.db.models import DeliveryStatus


async def _task(db_session):
    # Mirror how other repo tests create a task; adjust to the project's helper.
    from vts.db.models import User, Task, TaskStatus
    u = User(username=f"u-{uuid.uuid4().hex[:8]}")
    db_session.add(u); await db_session.flush()
    t = Task(user_id=u.id, source_url="http://x", options={},
             artifact_dir="/tmp/x", status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()
    return t


@pytest.mark.asyncio
async def test_claim_flips_to_delivering(db_session):
    repo = Repo(db_session)
    t = await _task(db_session)
    now = datetime.now(timezone.utc)
    await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="summary",
        max_attempts=3, next_attempt_at=now - timedelta(seconds=1))
    await db_session.commit()
    claimed = await repo.claim_due_deliveries(now, limit=10)
    assert len(claimed) == 1
    assert claimed[0].status == DeliveryStatus.delivering
    assert claimed[0].attempts == 1


@pytest.mark.asyncio
async def test_record_success(db_session):
    repo = Repo(db_session)
    t = await _task(db_session)
    now = datetime.now(timezone.utc)
    a = await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="raw",
        max_attempts=3, next_attempt_at=now)
    await db_session.commit()
    await repo.record_delivery_result(a.id, external_id="doc1", external_url="http://o/doc1")
    await db_session.commit()
    rows = await repo.list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/doc1"


@pytest.mark.asyncio
async def test_failure_dead_vs_retry(db_session):
    repo = Repo(db_session)
    t = await _task(db_session)
    now = datetime.now(timezone.utc)
    a = await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="raw",
        max_attempts=3, next_attempt_at=now)
    await db_session.commit()
    await repo.record_delivery_failure(a.id, last_error="boom",
                                       next_attempt_at=now + timedelta(seconds=60), dead=False)
    await db_session.commit()
    assert (await repo.list_deliveries_for_task(t.id))[0].status == DeliveryStatus.pending
    await repo.record_delivery_failure(a.id, last_error="boom2", next_attempt_at=None, dead=True)
    await db_session.commit()
    assert (await repo.list_deliveries_for_task(t.id))[0].status == DeliveryStatus.dead
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_delivery_attempts_repo.py -v`
Expected: FAIL — `AttributeError: ... 'create_delivery_attempt'`.

- [ ] **Step 3: Add repo methods**

In `vts/db/repo.py`, after the DeliveryTarget block (import `DeliveryAttempt`, `DeliveryStatus`, and `update`/`func` from sqlalchemy if not present):

```python
    # ------------------------------------------------------------------
    # DeliveryAttempt
    # ------------------------------------------------------------------

    async def create_delivery_attempt(
        self, *, task_id: uuid.UUID, target_id: uuid.UUID | None,
        adapter: str, variant: str, max_attempts: int,
        next_attempt_at: datetime,
    ) -> DeliveryAttempt:
        row = DeliveryAttempt(
            task_id=task_id, target_id=target_id, adapter=adapter, variant=variant,
            status=DeliveryStatus.pending, max_attempts=max_attempts,
            next_attempt_at=next_attempt_at)
        self.session.add(row)
        await self.session.flush()
        return row

    async def claim_due_deliveries(self, now: datetime, limit: int) -> list[DeliveryAttempt]:
        stmt = (select(DeliveryAttempt)
                .where(DeliveryAttempt.status == DeliveryStatus.pending,
                       DeliveryAttempt.next_attempt_at <= now)
                .order_by(DeliveryAttempt.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True))
        rows = list(await self.session.scalars(stmt))
        for row in rows:
            row.status = DeliveryStatus.delivering
            row.attempts += 1
        await self.session.flush()
        return rows

    async def record_delivery_result(
        self, attempt_id: uuid.UUID, *, external_id: str | None, external_url: str | None
    ) -> None:
        row = await self.session.get(DeliveryAttempt, attempt_id)
        if row is None:
            return
        row.status = DeliveryStatus.delivered
        row.external_id = external_id
        row.external_url = external_url
        row.last_error = None
        await self.session.flush()

    async def record_delivery_failure(
        self, attempt_id: uuid.UUID, *, last_error: str,
        next_attempt_at: datetime | None, dead: bool,
    ) -> None:
        row = await self.session.get(DeliveryAttempt, attempt_id)
        if row is None:
            return
        row.status = DeliveryStatus.dead if dead else DeliveryStatus.pending
        row.last_error = last_error[:2000]
        row.next_attempt_at = next_attempt_at
        await self.session.flush()

    async def list_deliveries_for_task(self, task_id: uuid.UUID) -> list[DeliveryAttempt]:
        stmt = (select(DeliveryAttempt)
                .where(DeliveryAttempt.task_id == task_id)
                .order_by(DeliveryAttempt.created_at))
        return list(await self.session.scalars(stmt))

    async def reap_stuck_deliveries(self, older_than: datetime) -> int:
        stmt = (update(DeliveryAttempt)
                .where(DeliveryAttempt.status == DeliveryStatus.delivering,
                       DeliveryAttempt.updated_at < older_than)
                .values(status=DeliveryStatus.pending))
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def reset_delivery_for_retry(
        self, task_id: uuid.UUID, target_id: uuid.UUID | None, now: datetime
    ) -> int:
        conds = [DeliveryAttempt.task_id == task_id,
                 DeliveryAttempt.status.in_([DeliveryStatus.failed, DeliveryStatus.dead])]
        if target_id is not None:
            conds.append(DeliveryAttempt.target_id == target_id)
        stmt = (update(DeliveryAttempt).where(*conds)
                .values(status=DeliveryStatus.pending, next_attempt_at=now))
        result = await self.session.execute(stmt)
        return result.rowcount or 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_delivery_attempts_repo.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_delivery_attempts_repo.py
git commit -m "feat(repo): DeliveryAttempt enqueue/claim/record/reap (vts-ouq)"
```

---

## Task 7: Variant resolution (content from artifacts)

**Files:**
- Create: `vts/delivery/resolve.py`
- Test: `tests/delivery/test_resolve.py`

**Interfaces:**
- Consumes: `DeliveryPayload`, `TaskMeta` (Task 2). A `Task` ORM object.
- Produces:
  - `resolve_variant(task, variant: str) -> DeliveryPayload` (raises `VariantUnavailable` if the file/path is missing)
  - `class VariantUnavailable(RuntimeError)`
  - `VALID_VARIANTS = ("raw", "redacted", "summary")`

Resolution mirrors how MCP `get_transcript`/summary read files:
- `raw` → `task.transcript_path` (txt).
- `summary` → `task.summary_path` (txt/markdown).
- `redacted` → the redacted transcript file under `Path(task.artifact_dir)/"outputs"/"transcript.redacted.txt"` (confirm exact name against `resolve_result_path`/MCP redacted handling during implementation; use the same helper the MCP layer uses rather than hardcoding if one exists).

`TaskMeta.language`/`duration_s` come from `task.options` (keys `detected_language`/`language`, and a duration key if present) — read defensively with `.get`, default `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/delivery/test_resolve.py
import types
from pathlib import Path
import pytest
from vts.delivery.resolve import resolve_variant, VariantUnavailable


def _fake_task(tmp_path, **over):
    t = types.SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        source_url="http://x", source_title="Title",
        artifact_dir=str(tmp_path),
        transcript_path=None, summary_path=None,
        options={"detected_language": "en"},
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    for k, v in over.items():
        setattr(t, k, v)
    return t


def test_raw_reads_transcript_path(tmp_path):
    p = tmp_path / "transcript.txt"; p.write_text("hello raw")
    t = _fake_task(tmp_path, transcript_path=str(p))
    payload = resolve_variant(t, "raw")
    assert payload.content == "hello raw"
    assert payload.variant == "raw"
    assert payload.task.language == "en"


def test_summary_reads_summary_path(tmp_path):
    p = tmp_path / "summary.md"; p.write_text("# sum")
    t = _fake_task(tmp_path, summary_path=str(p))
    assert resolve_variant(t, "summary").content == "# sum"


def test_missing_raises(tmp_path):
    t = _fake_task(tmp_path, transcript_path=None)
    with pytest.raises(VariantUnavailable):
        resolve_variant(t, "raw")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/delivery/test_resolve.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.delivery.resolve`.

- [ ] **Step 3: Write minimal implementation**

```python
# vts/delivery/resolve.py
from __future__ import annotations

from pathlib import Path

from vts.delivery.contract import DeliveryPayload, TaskMeta

VALID_VARIANTS = ("raw", "redacted", "summary")


class VariantUnavailable(RuntimeError):
    """The requested variant has no content for this task."""


def _task_meta(task) -> TaskMeta:
    opts = task.options or {}
    return TaskMeta(
        source_url=task.source_url,
        source_title=task.source_title,
        language=opts.get("detected_language") or opts.get("language"),
        duration_s=opts.get("duration_s"),
        created_at=task.created_at,
    )


def _read(path_str: str | None, *, variant: str) -> str:
    if not path_str:
        raise VariantUnavailable(f"{variant}: no path recorded")
    p = Path(path_str)
    if not p.exists():
        raise VariantUnavailable(f"{variant}: file missing at {p}")
    return p.read_text(encoding="utf-8")


def resolve_variant(task, variant: str) -> DeliveryPayload:
    if variant not in VALID_VARIANTS:
        raise VariantUnavailable(f"unknown variant: {variant}")
    if variant == "raw":
        content = _read(task.transcript_path, variant=variant)
        fmt = "txt"
    elif variant == "summary":
        content = _read(task.summary_path, variant=variant)
        fmt = "markdown"
    else:  # redacted
        redacted = Path(task.artifact_dir) / "outputs" / "transcript.redacted.txt"
        content = _read(str(redacted) if redacted.exists() else None, variant=variant)
        fmt = "txt"
    return DeliveryPayload(
        task_id=str(task.id), variant=variant, content=content,
        content_format=fmt, task=_task_meta(task))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/delivery/test_resolve.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vts/delivery/resolve.py tests/delivery/test_resolve.py
git commit -m "feat(delivery): resolve variant content from task artifacts (vts-ouq)"
```

---

## Task 8: Settings + backoff + enqueue on completion

**Files:**
- Modify: `vts/core/config.py`, `vts/delivery/queue.py` (create), `vts/pipeline/processor.py`
- Test: `tests/delivery/test_queue.py`, `tests/test_enqueue_on_completion.py`

**Interfaces:**
- Consumes: repo attempt methods (Task 6), `resolve_variant` (not here — used by consumer).
- Produces:
  - `Settings`: `delivery_max_attempts: int = 5`, `delivery_backoff_base_seconds: int = 60`, `delivery_backoff_cap_seconds: int = 3600`, `delivery_claim_batch: int = 10`, `delivery_stuck_seconds: int = 600`.
  - `vts/delivery/queue.py`:
    - `backoff_seconds(attempts: int, base: int, cap: int) -> int` → `min(base * 2**(attempts-1), cap)`
    - `async def enqueue_deliveries(repo, task, *, max_attempts: int, now) -> int` — reads `task.options.get("delivery")` (a list of `{deliver_to, variant?}`), resolves each `deliver_to` to a `DeliveryTarget` by name for `task.user_id`, and creates one `DeliveryAttempt` per resolved target (`variant` = element's `variant` or `target.config_json.get("default_variant", "summary")`). Returns count enqueued. Unknown target names are skipped with a logged warning (validation already happened at submit; this is defensive).

- [ ] **Step 1: Write the failing test (backoff + enqueue)**

```python
# tests/delivery/test_queue.py
import uuid
from datetime import datetime, timezone
import pytest
from vts.delivery.queue import backoff_seconds, enqueue_deliveries


def test_backoff_progression():
    assert backoff_seconds(1, 60, 3600) == 60
    assert backoff_seconds(2, 60, 3600) == 120
    assert backoff_seconds(3, 60, 3600) == 240
    assert backoff_seconds(100, 60, 3600) == 3600  # capped


@pytest.mark.asyncio
async def test_enqueue_creates_attempt_per_target(db_session):
    from vts.db.repo import Repo
    from vts.db.models import User, Task, TaskStatus
    repo = Repo(db_session)
    u = User(username=f"u-{uuid.uuid4().hex[:8]}"); db_session.add(u); await db_session.flush()
    await repo.create_delivery_target(u.id, name="out", adapter="fake",
                                      config={"default_variant": "summary"}, secrets_enc=None)
    t = Task(user_id=u.id, source_url="http://x",
             options={"delivery": [{"deliver_to": "out"}]},
             artifact_dir="/tmp/x", status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()
    n = await enqueue_deliveries(repo, t, max_attempts=5, now=datetime.now(timezone.utc))
    assert n == 1
    rows = await repo.list_deliveries_for_task(t.id)
    assert rows[0].adapter == "fake" and rows[0].variant == "summary"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/delivery/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.delivery.queue`.

- [ ] **Step 3: Add settings**

In `vts/core/config.py` `class Settings`:

```python
    delivery_max_attempts: int = 5
    delivery_backoff_base_seconds: int = 60
    delivery_backoff_cap_seconds: int = 3600
    delivery_claim_batch: int = 10
    delivery_stuck_seconds: int = 600
```

- [ ] **Step 4: Write `vts/delivery/queue.py`**

```python
# vts/delivery/queue.py
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("vts.delivery")


def backoff_seconds(attempts: int, base: int, cap: int) -> int:
    return min(base * (2 ** (attempts - 1)), cap)


async def enqueue_deliveries(repo, task, *, max_attempts: int, now: datetime) -> int:
    spec = (task.options or {}).get("delivery") or []
    if not isinstance(spec, list):
        return 0
    count = 0
    for item in spec:
        if not isinstance(item, dict):
            continue
        name = item.get("deliver_to")
        if not name:
            continue
        target = await repo.get_delivery_target_by_name(task.user_id, name)
        if target is None:
            logger.warning("delivery target %r not found for task %s; skipping", name, task.id)
            continue
        variant = item.get("variant") or (target.config_json or {}).get("default_variant", "summary")
        await repo.create_delivery_attempt(
            task_id=task.id, target_id=target.id, adapter=target.adapter,
            variant=variant, max_attempts=max_attempts, next_attempt_at=now)
        count += 1
    return count
```

- [ ] **Step 5: Run queue test to verify it passes**

Run: `uv run pytest tests/delivery/test_queue.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Wire enqueue into `process_task` (deliver_safe)**

In `vts/pipeline/processor.py`, immediately after the `send_push_safe(...)` call in the success branch (around line 190-198), add a guarded enqueue + Redis notify:

```python
                try:
                    from vts.delivery.queue import enqueue_deliveries
                    from vts.db.models import utcnow as _utcnow
                    n = await enqueue_deliveries(
                        repo, task,
                        max_attempts=self.settings.delivery_max_attempts,
                        now=_utcnow())
                    if n:
                        await session.commit()
                        await self.redis.publish(
                            f"{self.settings.redis_prefix}delivery:notify", "1")
                except Exception:
                    logger.exception("delivery enqueue failed for task %s (task stays completed)", task.id)
```

- [ ] **Step 7: Write the completion-enqueue integration test**

```python
# tests/test_enqueue_on_completion.py
import uuid
from datetime import datetime, timezone
import pytest
from vts.delivery.queue import enqueue_deliveries
from vts.db.repo import Repo
from vts.db.models import User, Task, TaskStatus


@pytest.mark.asyncio
async def test_enqueue_skips_unknown_target_without_raising(db_session):
    repo = Repo(db_session)
    u = User(username=f"u-{uuid.uuid4().hex[:8]}"); db_session.add(u); await db_session.flush()
    t = Task(user_id=u.id, source_url="http://x",
             options={"delivery": [{"deliver_to": "does-not-exist"}]},
             artifact_dir="/tmp/x", status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()
    n = await enqueue_deliveries(repo, t, max_attempts=5, now=datetime.now(timezone.utc))
    assert n == 0
    assert await repo.list_deliveries_for_task(t.id) == []
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/delivery/test_queue.py tests/test_enqueue_on_completion.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add vts/core/config.py vts/delivery/queue.py vts/pipeline/processor.py tests/delivery/test_queue.py tests/test_enqueue_on_completion.py
git commit -m "feat(delivery): settings, backoff, enqueue-on-completion (deliver_safe) (vts-ouq)"
```

---

## Task 9: Consumer loop (claim → deliver → record) + reaper

> **SPEC UPDATE 2026-07-31 (commit cce964c) — applies to this task.** A temporarily unavailable adapter
> (plugin did not load this restart) is a normal transient state, NOT a delivery failure. Therefore:
> - Add `waiting_adapter = "waiting_adapter"` to `DeliveryStatus` in `vts/db/models.py`. **No migration
>   needed** — the `status` column is `sa.String(32)` / `native_enum=False`, so a new value is
>   Python-side only.
> - Add `delivery_adapter_wait_seconds: int = 300` to `Settings` (recheck interval for parked rows).
> - In `process_one_delivery`: call `get_adapter(attempt.adapter)` **FIRST** inside the `try`, before
>   resolving the variant or decrypting secrets. Catch `UnknownAdapter` in its **own** `except` BEFORE
>   the generic one: set `status=waiting_adapter`, **decrement `attempts` back** (the attempt was not
>   spent), `next_attempt_at = now + delivery_adapter_wait_seconds`. Never `dead`, never `last_error`
>   noise. Requires a repo method (e.g. `park_delivery_for_adapter(attempt_id, next_attempt_at)`).
> - `claim_due_deliveries` must claim `status IN (pending, waiting_adapter)` — otherwise parked rows
>   never wake up. This changes the Task 6 repo method; update it here.
> - Test additionally: unregistered adapter → `waiting_adapter`, `attempts` stays 0, not `dead` after
>   max_attempts; then register the adapter → next tick delivers.

**Files:**
- Create: `vts/delivery/consumer.py`
- Modify: `vts/worker/main.py`
- Test: `tests/delivery/test_consumer.py`

**Interfaces:**
- Consumes: `claim_due_deliveries`, `record_delivery_result`, `record_delivery_failure`, `reap_stuck_deliveries` (Task 6); `resolve_variant` (Task 7); `get_adapter` (Task 3); `decrypt_secrets` + `load_secrets_key` (Task 1); `backoff_seconds`, settings (Task 8).
- Produces:
  - `async def process_one_delivery(session_factory, settings, attempt_id) -> None` — loads attempt+task+target, resolves variant, decrypts secrets, calls adapter, records result/failure with backoff. **Isolated + unit-testable.**
  - `async def delivery_tick(session_factory, settings, now) -> int` — reap stuck, claim a batch, run each via `process_one_delivery`, return number processed.
  - `async def delivery_loop(session_factory, settings, redis) -> None` — subscribe `delivery:notify`, loop `delivery_tick`, sleep/wakeup like `worker_loop`.

Note: claim flips rows to `delivering` and commits (so other workers skip them); each attempt is then processed in its **own** session so one failure can't roll back another's status write.

- [ ] **Step 1: Write the failing test (success + failure paths, fake adapter)**

```python
# tests/delivery/test_consumer.py
import uuid
from datetime import datetime, timezone
import pytest
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult, DeliveryError
from vts.delivery.consumer import delivery_tick
from vts.db.repo import Repo
from vts.db.models import User, Task, TaskStatus, DeliveryStatus


class OkAdapter:
    name = "ok"
    def config_schema(self): return {}
    def secret_keys(self): return []
    async def deliver(self, payload, target):
        return DeliveryResult(external_id="doc9", external_url="http://o/doc9")


class BoomAdapter:
    name = "boom"
    def config_schema(self): return {}
    def secret_keys(self): return []
    async def deliver(self, payload, target):
        raise DeliveryError("nope")


@pytest.fixture
def _settings():
    from vts.core.config import get_settings
    return get_settings()


async def _completed_task_with_raw(db_session, tmp_path, adapter_name):
    repo = Repo(db_session)
    u = User(username=f"u-{uuid.uuid4().hex[:8]}"); db_session.add(u); await db_session.flush()
    p = tmp_path / "transcript.txt"; p.write_text("body")
    t = Task(user_id=u.id, source_url="http://x", options={},
             artifact_dir=str(tmp_path), transcript_path=str(p),
             status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()
    await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter=adapter_name, variant="raw",
        max_attempts=2, next_attempt_at=datetime.now(timezone.utc))
    await db_session.commit()
    return t


@pytest.mark.asyncio
async def test_tick_delivers_success(db_session, tmp_path, monkeypatch, _settings, session_factory):
    monkeypatch.setattr(registry, "_CACHE", {"ok": OkAdapter()}, raising=False)
    t = await _completed_task_with_raw(db_session, tmp_path, "ok")
    await delivery_tick(session_factory, _settings, datetime.now(timezone.utc))
    rows = await Repo(db_session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/doc9"


@pytest.mark.asyncio
async def test_tick_failure_retries_then_dead(db_session, tmp_path, monkeypatch, _settings, session_factory):
    monkeypatch.setattr(registry, "_CACHE", {"boom": BoomAdapter()}, raising=False)
    t = await _completed_task_with_raw(db_session, tmp_path, "boom")
    now = datetime.now(timezone.utc)
    await delivery_tick(session_factory, _settings, now)  # attempt 1 → pending (retry)
    rows = await Repo(db_session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.pending
    # force it due again and exhaust max_attempts=2
    await Repo(db_session).record_delivery_failure(rows[0].id, last_error="x", next_attempt_at=now, dead=False)
    await db_session.commit()
    await delivery_tick(session_factory, _settings, now)  # attempt 2 → dead
    rows = await Repo(db_session).list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.dead
```

(Use the project's `session_factory` fixture; if none exists, add one to `tests/conftest.py` that yields the app's `async_sessionmaker`. Match how other multi-session tests obtain a factory.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/delivery/test_consumer.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.delivery.consumer`.

- [ ] **Step 3: Write `vts/delivery/consumer.py`**

```python
# vts/delivery/consumer.py
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

from vts.core.secrets import decrypt_secrets, load_secrets_key, SecretsKeyMissing
from vts.db.models import utcnow
from vts.db.repo import Repo
from vts.delivery.contract import DeliveryTargetConfig
from vts.delivery.queue import backoff_seconds
from vts.delivery.registry import get_adapter, UnknownAdapter
from vts.delivery.resolve import resolve_variant, VariantUnavailable

logger = logging.getLogger("vts.delivery")


async def process_one_delivery(session_factory, settings, attempt_id) -> None:
    async with session_factory() as session:
        repo = Repo(session)
        attempt = await repo.session.get(__import__("vts.db.models", fromlist=["DeliveryAttempt"]).DeliveryAttempt, attempt_id)
        if attempt is None:
            return
        task = await repo.get_task_by_id(attempt.task_id)
        target = (await repo.get_delivery_target(task.user_id, attempt.target_id)
                  if attempt.target_id else None)
        now = utcnow()
        try:
            if task is None:
                raise VariantUnavailable("task gone")
            payload = resolve_variant(task, attempt.variant)
            adapter = get_adapter(attempt.adapter)
            secrets: dict[str, str] = {}
            if target is not None and target.secrets_enc:
                secrets = decrypt_secrets(target.secrets_enc, load_secrets_key(settings))
            cfg = DeliveryTargetConfig(
                config=(target.config_json if target else {}) or {}, secrets=secrets)
            result = await adapter.deliver(payload, cfg)
            await repo.record_delivery_result(
                attempt.id, external_id=result.external_id, external_url=result.external_url)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 — any failure is retryable
            dead = attempt.attempts >= attempt.max_attempts
            next_at = None if dead else now + timedelta(
                seconds=backoff_seconds(attempt.attempts,
                                        settings.delivery_backoff_base_seconds,
                                        settings.delivery_backoff_cap_seconds))
            await repo.record_delivery_failure(
                attempt.id, last_error=f"{type(exc).__name__}: {exc}",
                next_attempt_at=next_at, dead=dead)
            await session.commit()
            logger.warning("delivery %s failed (attempt %s/%s, dead=%s): %s",
                           attempt.id, attempt.attempts, attempt.max_attempts, dead, exc)


async def delivery_tick(session_factory, settings, now: datetime) -> int:
    async with session_factory() as session:
        repo = Repo(session)
        await repo.reap_stuck_deliveries(now - timedelta(seconds=settings.delivery_stuck_seconds))
        claimed = await repo.claim_due_deliveries(now, limit=settings.delivery_claim_batch)
        await session.commit()
        ids = [row.id for row in claimed]
    for attempt_id in ids:
        await process_one_delivery(session_factory, settings, attempt_id)
    return len(ids)


async def delivery_loop(session_factory, settings, redis) -> None:
    notify_channel = f"{settings.redis_prefix}delivery:notify"
    pubsub = redis.pubsub()
    await pubsub.subscribe(notify_channel)
    wakeup = asyncio.Event()

    async def _pump():
        async for _ in pubsub.listen():
            wakeup.set()

    pump = asyncio.create_task(_pump())
    try:
        while True:
            processed = await delivery_tick(session_factory, settings, utcnow())
            if not processed:
                wakeup.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=5.0)
            else:
                await asyncio.sleep(0.2)
    finally:
        pump.cancel()
        with suppress(BaseException):
            await pump
        with suppress(Exception):
            await pubsub.unsubscribe(notify_channel)
            await pubsub.aclose()
```

(During implementation, replace the `__import__` line with a clean top-level `from vts.db.models import DeliveryAttempt` — it's written inline here only to keep the import list explicit; use the clean import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/delivery/test_consumer.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Start the loop in the worker**

In `vts/worker/main.py` `worker_loop`, alongside `upload_gc_task`, add:

```python
    delivery_task: asyncio.Task[None] | None = None
```
and after the `upload_gc_task` creation:
```python
        from vts.delivery.consumer import delivery_loop
        delivery_task = asyncio.create_task(
            delivery_loop(SessionLocal, settings, redis))
```
and in the `finally` block, mirror the other task teardowns:
```python
        if delivery_task is not None:
            delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await delivery_task
```

- [ ] **Step 6: Run the delivery test module + worker import smoke**

Run: `uv run pytest tests/delivery/ -v && uv run python -c "import vts.worker.main"`
Expected: PASS; import OK.

- [ ] **Step 7: Commit**

```bash
git add vts/delivery/consumer.py vts/worker/main.py tests/delivery/test_consumer.py
git commit -m "feat(delivery): consumer loop with backoff/dead + reaper, wired into worker (vts-ouq)"
```

---

## Task 10: DeliveryTarget REST endpoints (write-only secrets)

> **SPEC UPDATE 2026-07-31 (commit cce964c).** Add `adapter_available: bool` to `DeliveryTargetOut`
> (computed via `registry`: is this target's adapter currently loaded). A target whose plugin is missing
> stays listed and is merely flagged — list/get must never fail because of it (the existing
> `except UnknownAdapter` around `secret_keys()` already covers that). Test that a target with an
> unregistered adapter still lists, with `adapter_available=false`.

**Files:**
- Modify: `vts/api/schemas.py`, `vts/api/main.py`
- Test: `tests/test_delivery_targets_api.py`

**Interfaces:**
- Consumes: repo target CRUD (Task 5), `encrypt_secrets`/`load_secrets_key` (Task 1), `list_adapters`/`get_adapter` (Task 3).
- Produces REST:
  - `POST /api/delivery-targets` → `DeliveryTargetOut`
  - `GET /api/delivery-targets` → `list[DeliveryTargetOut]`
  - `GET /api/delivery-targets/{id}` → `DeliveryTargetOut`
  - `PUT /api/delivery-targets/{id}` → `DeliveryTargetOut`
  - `DELETE /api/delivery-targets/{id}` → 204
  - Schemas: `DeliveryTargetCreate` (`name`, `adapter`, `config: dict`, `secrets: dict[str,str] | None`), `DeliveryTargetUpdate` (`name?`, `config?`, `secrets?`, `clear_secrets: bool = False`), `DeliveryTargetOut` (`id`, `name`, `adapter`, `config`, `secrets: dict[str, dict]` presence markers — never values).

`DeliveryTargetOut.secrets` is built from the adapter's `secret_keys()`: `{key: {"set": <key present in stored blob>}}`. To know which keys are set without exposing values, decrypt server-side and emit only booleans. If `VTS_SECRETS_KEY` is unset and a target has secrets, return `{key: {"set": True}}` optimistically (never the value) and do not fail the GET.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delivery_targets_api.py
import pytest
# Use the project's existing API test client + auth fixtures (see tests/test_presets_api.py).


@pytest.mark.asyncio
async def test_create_hides_secret_value(client, auth_headers, monkeypatch):
    # ensure a secrets key exists
    from vts.core.config import get_settings
    from cryptography.fernet import Fernet
    monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode(), raising=False)

    resp = await client.post("/api/delivery-targets", headers=auth_headers, json={
        "name": "out", "adapter": "outline",
        "config": {"collection_id": "c1"},
        "secrets": {"api_token": "supersecret"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "out"
    assert "supersecret" not in resp.text
    assert body["secrets"]["api_token"] == {"set": True}


@pytest.mark.asyncio
async def test_list_never_returns_secret_values(client, auth_headers):
    resp = await client.get("/api/delivery-targets", headers=auth_headers)
    assert resp.status_code == 200
    assert "supersecret" not in resp.text
```

(Register a `FakeOutline`-style adapter for the test via `monkeypatch.setattr(registry, "_CACHE", {...})` if adapter validation rejects unknown `outline`. Match the create endpoint's validation.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_delivery_targets_api.py -v`
Expected: FAIL — 404 (routes not defined).

- [ ] **Step 3: Add schemas**

In `vts/api/schemas.py`:

```python
class DeliveryTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    adapter: str = Field(min_length=1, max_length=64)
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str] | None = None


class DeliveryTargetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    config: dict | None = None
    secrets: dict[str, str] | None = None
    clear_secrets: bool = False


class DeliveryTargetOut(BaseModel):
    id: str
    name: str
    adapter: str
    config: dict
    secrets: dict[str, dict]  # {key: {"set": bool}} — never values
```

- [ ] **Step 4: Add endpoints + a helper**

In `vts/api/main.py`, near the presets endpoints, add a helper and the five routes. Helper builds `DeliveryTargetOut` with presence-only secrets:

```python
    def _delivery_target_out(target) -> DeliveryTargetOut:
        from vts.delivery.registry import get_adapter, UnknownAdapter
        try:
            keys = get_adapter(target.adapter).secret_keys()
        except UnknownAdapter:
            keys = []
        stored: dict = {}
        if target.secrets_enc:
            try:
                from vts.core.secrets import decrypt_secrets, load_secrets_key
                stored = decrypt_secrets(target.secrets_enc, load_secrets_key(settings))
            except Exception:
                stored = {k: True for k in keys}  # key unavailable — presence optimistic, no values
        return DeliveryTargetOut(
            id=str(target.id), name=target.name, adapter=target.adapter,
            config=target.config_json or {},
            secrets={k: {"set": bool(stored.get(k))} for k in keys})
```

Create route (validates adapter + config, encrypts secrets):

```python
    @app.post("/api/delivery-targets", response_model=DeliveryTargetOut)
    async def create_delivery_target_endpoint(
        payload: DeliveryTargetCreate,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep)) -> DeliveryTargetOut:
        from vts.delivery.registry import get_adapter, UnknownAdapter
        try:
            get_adapter(payload.adapter)
        except UnknownAdapter:
            raise HTTPException(status_code=400, detail=f"Unknown delivery adapter: {payload.adapter}")
        secrets_enc = None
        if payload.secrets:
            from vts.core.secrets import encrypt_secrets, load_secrets_key
            secrets_enc = encrypt_secrets(payload.secrets, load_secrets_key(settings))
        repo = Repo(session)
        target = await repo.create_delivery_target(
            uuid.UUID(user.id), name=payload.name.strip(), adapter=payload.adapter,
            config=payload.config, secrets_enc=secrets_enc)
        await session.commit()
        return _delivery_target_out(target)
```

Add `GET` (list + by id), `PUT` (update; encrypt secrets only if provided, pass `clear_secrets`), `DELETE` (204) following the exact shapes of the presets endpoints and repo methods from Task 5. In `PUT`, encrypt `payload.secrets` when present; pass `secrets_enc=None, clear_secrets=payload.clear_secrets` otherwise.

Import the new schemas at the top of `main.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_delivery_targets_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vts/api/schemas.py vts/api/main.py tests/test_delivery_targets_api.py
git commit -m "feat(api): DeliveryTarget REST CRUD with write-only secrets (vts-ouq)"
```

---

## Task 11: Task deliveries REST (status + retry)

**Files:**
- Modify: `vts/api/schemas.py`, `vts/api/main.py`
- Test: `tests/test_task_deliveries_api.py`

**Interfaces:**
- Consumes: `list_deliveries_for_task`, `reset_delivery_for_retry` (Task 6).
- Produces:
  - `GET /api/tasks/{task_id}/deliveries` → `list[DeliveryOut]`
  - `POST /api/tasks/{task_id}/deliveries/retry` (optional body `{target_id?}`) → `{reset: int}` (also publishes `delivery:notify`)
  - Schema `DeliveryOut`: `id`, `adapter`, `variant`, `status`, `attempts`, `max_attempts`, `last_error`, `external_url`.

Ownership: verify the task belongs to the current user before listing/retrying (404 otherwise). `last_error` is safe to expose (it's an error string, never a secret — the consumer records `f"{type}: {exc}"`; adapters must not put secrets in exception messages — note this as an adapter guideline).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_deliveries_api.py
import pytest
# Reuse API client/auth fixtures; create a task owned by the user and an attempt row.


@pytest.mark.asyncio
async def test_list_and_retry(client, auth_headers, db_session, current_user_id):
    from vts.db.repo import Repo
    from vts.db.models import Task, TaskStatus, DeliveryStatus
    import uuid
    from datetime import datetime, timezone
    repo = Repo(db_session)
    t = Task(user_id=uuid.UUID(current_user_id), source_url="http://x",
             options={}, artifact_dir="/tmp/x", status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()
    a = await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="raw",
        max_attempts=2, next_attempt_at=datetime.now(timezone.utc))
    await repo.record_delivery_failure(a.id, last_error="boom", next_attempt_at=None, dead=True)
    await db_session.commit()

    resp = await client.get(f"/api/tasks/{t.id}/deliveries", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "dead"

    resp = await client.post(f"/api/tasks/{t.id}/deliveries/retry", headers=auth_headers, json={})
    assert resp.status_code == 200
    assert resp.json()["reset"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_task_deliveries_api.py -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add schema + endpoints**

`DeliveryOut` in `vts/api/schemas.py`:

```python
class DeliveryOut(BaseModel):
    id: str
    adapter: str
    variant: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    external_url: str | None
```

Endpoints in `vts/api/main.py` (verify ownership via existing task-fetch helper; publish notify on retry through the app's redis handle — match how other endpoints access redis, e.g. the cancel/pause endpoints):

```python
    @app.get("/api/tasks/{task_id}/deliveries", response_model=list[DeliveryOut])
    async def list_task_deliveries_endpoint(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep)) -> list[DeliveryOut]:
        repo = Repo(session)
        task = await repo.get_task_by_id(task_id)
        if task is None or str(task.user_id) != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        rows = await repo.list_deliveries_for_task(task_id)
        return [DeliveryOut(id=str(r.id), adapter=r.adapter, variant=r.variant,
                            status=r.status.value, attempts=r.attempts,
                            max_attempts=r.max_attempts, last_error=r.last_error,
                            external_url=r.external_url) for r in rows]

    @app.post("/api/tasks/{task_id}/deliveries/retry")
    async def retry_task_deliveries_endpoint(
        task_id: uuid.UUID, body: dict | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep)) -> dict:
        from vts.db.models import utcnow as _utcnow
        repo = Repo(session)
        task = await repo.get_task_by_id(task_id)
        if task is None or str(task.user_id) != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        target_id = None
        if body and body.get("target_id"):
            target_id = uuid.UUID(body["target_id"])
        n = await repo.reset_delivery_for_retry(task_id, target_id, _utcnow())
        await session.commit()
        # wake the consumer (match how other endpoints reach redis/bus)
        await request_app_redis_publish(f"{settings.redis_prefix}delivery:notify", "1")
        return {"reset": n}
```

(Replace `request_app_redis_publish` with the actual redis/bus access pattern used elsewhere in `main.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_task_deliveries_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/api/schemas.py vts/api/main.py tests/test_task_deliveries_api.py
git commit -m "feat(api): task delivery status + retry endpoints (vts-ouq)"
```

---

## Task 12: MCP surface — target CRUD, submit.delivery, status, retry + preset allowlist

> **SPEC UPDATE 2026-07-31 (commit cce964c).** Submit validation gains an availability check: an
> **explicitly submitted** `deliver_to` whose adapter is not currently loaded → clear error (REST + MCP).
> The SAME target arriving from a **preset** must NOT fail the task — it is enqueued and parked in
> `waiting_adapter` (Task 9). Keep those two paths distinct; test both.

**Files:**
- Modify: `vts/mcp/tools.py`, `vts/mcp/schemas.py`, `vts/mcp/server.py`, `vts/services/preset_expand.py`, `vts/api/schemas.py`
- Test: `tests/mcp/test_tools_delivery.py`, `tests/test_preset_delivery_allowlist.py`

**Interfaces:**
- Consumes: repo target/attempt methods, encryption, registry.
- Produces:
  - MCP tools: `create_delivery_target`, `list_delivery_targets`, `update_delivery_target`, `delete_delivery_target`, `get_delivery_status`, `retry_delivery`.
  - `submit_video` gains an optional `delivery: list[dict] | None` param → written into `task.options["delivery"]`; validated: each `deliver_to` must resolve to one of the user's targets, else the tool returns an error. Submit-provided `delivery` **replaces** any preset-provided `delivery` (field-level replace, consistent with other options).
  - `PresetOptions` (`vts/api/schemas.py`) gains `delivery: list[dict] = Field(default_factory=list)`.
  - `expand_preset_options` adds `"delivery": o.get("delivery", [])` to its returned dict.

- [ ] **Step 1: Write the failing test (preset allowlist — the easy-to-miss trap)**

```python
# tests/test_preset_delivery_allowlist.py
from vts.services.preset_expand import expand_preset_options


def test_delivery_survives_expansion():
    opts = {"delivery": [{"deliver_to": "out", "variant": "summary"}]}
    out = expand_preset_options(opts, valid_user_prompt_ids=set())
    assert out["delivery"] == [{"deliver_to": "out", "variant": "summary"}]


def test_delivery_defaults_empty():
    out = expand_preset_options({}, valid_user_prompt_ids=set())
    assert out["delivery"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preset_delivery_allowlist.py -v`
Expected: FAIL — `KeyError: 'delivery'`.

- [ ] **Step 3: Add `delivery` to the allowlist + PresetOptions**

In `vts/services/preset_expand.py` `expand_preset_options`, add to the returned dict:

```python
        "delivery": o.get("delivery", []),
```

In `vts/api/schemas.py` `class PresetOptions`, add:

```python
    delivery: list[dict] = Field(default_factory=list)
```

- [ ] **Step 4: Run the allowlist test to verify it passes**

Run: `uv run pytest tests/test_preset_delivery_allowlist.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write the failing MCP tools test**

```python
# tests/mcp/test_tools_delivery.py
import uuid
import pytest
# Follow tests/mcp/test_tools_prompts.py for how tools + fake repo/user are wired.


@pytest.mark.asyncio
async def test_create_and_list_target_hides_secrets(mcp_ctx):
    # mcp_ctx provides user + repo + settings analogous to prompt tool tests
    created = await mcp_ctx.tools.create_delivery_target(
        name="out", adapter="fake", config={"default_variant": "summary"},
        secrets={"token": "s3cr3t"})
    assert "s3cr3t" not in str(created)
    listing = await mcp_ctx.tools.list_delivery_targets()
    assert any(t["name"] == "out" for t in listing)
    assert all("s3cr3t" not in str(t) for t in listing)


@pytest.mark.asyncio
async def test_submit_delivery_unknown_target_errors(mcp_ctx):
    with pytest.raises(Exception):
        await mcp_ctx.tools.submit_video(url="http://x", delivery=[{"deliver_to": "nope"}])
```

(Model `mcp_ctx` on the existing MCP tool test harness. If tools are plain functions taking repo/user, call them that way instead of via a wrapper.)

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/mcp/test_tools_delivery.py -v`
Expected: FAIL — tool functions not defined.

- [ ] **Step 7: Implement MCP tools + submit.delivery**

In `vts/mcp/tools.py` add the six tool functions (mirroring the prompt/preset MCP tools' signatures and repo usage). Secrets are encrypted on write via `encrypt_secrets(load_secrets_key(settings))`; list/get return presence markers only (reuse the same `{key: {"set": bool}}` shaping as REST — factor a small helper if convenient). Extend the `submit_video` tool to accept `delivery: list[dict] | None`; when provided, validate each `deliver_to` against `repo.get_delivery_target_by_name(user_id, name)` (raise/return error if missing) and set `options["delivery"] = delivery` (replacing any preset value). Register the new tools in `vts/mcp/server.py` with `@mcp.tool(name=...)` wrappers exactly like `create_prompt`/`list_presets`.

Add MCP result schemas to `vts/mcp/schemas.py` as needed (e.g. `DeliveryTargetInfo`, `DeliveryStatusInfo`) — never include secret values.

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/test_tools_delivery.py tests/test_preset_delivery_allowlist.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add vts/mcp/ vts/services/preset_expand.py vts/api/schemas.py tests/mcp/test_tools_delivery.py tests/test_preset_delivery_allowlist.py
git commit -m "feat(mcp): delivery target CRUD, submit.delivery, status/retry; preset allowlist (vts-ouq)"
```

---

## Task 13: Outline adapter (separate package `vts-outline`)

**Files:**
- Create: `vts-outline/pyproject.toml`, `vts-outline/vts_outline/__init__.py`, `vts-outline/tests/test_outline_adapter.py`
- Modify: `Dockerfile` (install the package into the image)

**Interfaces:**
- Consumes: `DeliveryPayload`, `DeliveryTargetConfig`, `DeliveryResult`, `DeliveryError` from `vts.delivery.contract`.
- Produces: `OutlineAdapter` with `name="outline"`, `config_schema()`, `secret_keys()=["api_token"]`, `async deliver(...)`. Registered via entry point `outline = "vts_outline:OutlineAdapter"`.

`deliver` behavior:
- `config`: `base_url` (Outline API base), `collection_id`, `default_variant` (informational; variant already resolved).
- Title = `payload.task.source_title or payload.task.source_url`.
- Body = `payload.content` (already markdown for summary; wrap raw/redacted txt in a fenced or plain block + a small metadata header with source_url/language).
- **Idempotency:** if `payload`-derived external id is already known (the consumer passes the attempt's stored `external_id`? No — the adapter is stateless). Instead: on first delivery create a document and return its id/url; on retry the consumer calls `deliver` again — to avoid duplicates, the adapter searches Outline for an existing document whose title+collection matches a deterministic key (e.g. include `task_id` in the document's metadata/slug) and updates it if found. Document the deterministic key: embed `vts-task:{task_id}` in the document text footer and search by it before creating.
- On any HTTP/API error raise `DeliveryError` (consumer retries).

- [ ] **Step 1: Write the failing test (mocked Outline API)**

```python
# vts-outline/tests/test_outline_adapter.py
import pytest
import respx
import httpx
from datetime import datetime, timezone
from vts.delivery.contract import DeliveryPayload, DeliveryTargetConfig, TaskMeta
from vts_outline import OutlineAdapter


def _payload():
    meta = TaskMeta(source_url="http://v/1", source_title="Meeting",
                    language="en", duration_s=60.0,
                    created_at=datetime.now(timezone.utc))
    return DeliveryPayload(task_id="t-1", variant="summary",
                           content="# Notes\nbody", content_format="markdown", task=meta)


@pytest.mark.asyncio
@respx.mock
async def test_creates_document():
    base = "https://outline.example/api"
    respx.post(f"{base}/documents.search").mock(
        return_value=httpx.Response(200, json={"data": []}))
    respx.post(f"{base}/documents.create").mock(
        return_value=httpx.Response(200, json={"data": {"id": "doc1", "url": "/doc/doc1"}}))
    adapter = OutlineAdapter()
    result = await adapter.deliver(
        _payload(),
        DeliveryTargetConfig(config={"base_url": base, "collection_id": "c1"},
                             secrets={"api_token": "tok"}))
    assert result.external_id == "doc1"
    assert "doc1" in (result.external_url or "")


@pytest.mark.asyncio
@respx.mock
async def test_api_error_raises_delivery_error():
    from vts.delivery.contract import DeliveryError
    base = "https://outline.example/api"
    respx.post(f"{base}/documents.search").mock(return_value=httpx.Response(500))
    adapter = OutlineAdapter()
    with pytest.raises(DeliveryError):
        await adapter.deliver(
            _payload(),
            DeliveryTargetConfig(config={"base_url": base, "collection_id": "c1"},
                                 secrets={"api_token": "tok"}))
```

Add `respx` to the `vts-outline` dev deps.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vts-outline && uv run pytest tests/ -v` (or `pip install -e . && pytest`)
Expected: FAIL — package/module not found.

- [ ] **Step 3: Write `vts-outline/pyproject.toml`**

```toml
[project]
name = "vts-outline"
version = "0.1.0"
description = "Outline delivery adapter for VTS"
requires-python = ">=3.14"
dependencies = ["vts", "httpx>=0.27"]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "respx"]

[project.entry-points."vts.delivery"]
outline = "vts_outline:OutlineAdapter"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 4: Write `vts-outline/vts_outline/__init__.py`**

```python
from __future__ import annotations

import httpx

from vts.delivery.contract import (
    DeliveryError, DeliveryPayload, DeliveryResult, DeliveryTargetConfig,
)

_MARKER_PREFIX = "vts-task:"


class OutlineAdapter:
    name = "outline"

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "collection_id": {"type": "string"},
                "default_variant": {"type": "string",
                                    "enum": ["raw", "redacted", "summary"]},
            },
            "required": ["base_url", "collection_id"],
        }

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def _title(self, payload: DeliveryPayload) -> str:
        return payload.task.source_title or payload.task.source_url

    def _body(self, payload: DeliveryPayload) -> str:
        header = f"> source: {payload.task.source_url}"
        if payload.task.language:
            header += f" · lang: {payload.task.language}"
        marker = f"\n\n<!-- {_MARKER_PREFIX}{payload.task_id} -->"
        return f"{header}\n\n{payload.content}{marker}"

    async def deliver(self, payload: DeliveryPayload,
                      target: DeliveryTargetConfig) -> DeliveryResult:
        base = target.config["base_url"].rstrip("/")
        collection_id = target.config["collection_id"]
        token = target.secrets.get("api_token", "")
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        marker = f"{_MARKER_PREFIX}{payload.task_id}"
        try:
            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                # Idempotency: find an existing doc carrying this task's marker.
                found = await client.post(f"{base}/documents.search",
                                          json={"query": marker})
                if found.status_code >= 400:
                    raise DeliveryError(f"Outline search failed: {found.status_code}")
                data = found.json().get("data") or []
                existing_id = None
                for hit in data:
                    doc = hit.get("document") or hit
                    if marker in (doc.get("text") or ""):
                        existing_id = doc.get("id")
                        break

                if existing_id:
                    resp = await client.post(f"{base}/documents.update", json={
                        "id": existing_id, "title": self._title(payload),
                        "text": self._body(payload)})
                else:
                    resp = await client.post(f"{base}/documents.create", json={
                        "collectionId": collection_id, "title": self._title(payload),
                        "text": self._body(payload), "publish": True})
                if resp.status_code >= 400:
                    raise DeliveryError(f"Outline write failed: {resp.status_code} {resp.text}")
                doc = resp.json().get("data") or {}
                url = doc.get("url")
                full_url = (base.replace("/api", "") + url) if url and url.startswith("/") else url
                return DeliveryResult(external_id=doc.get("id"), external_url=full_url)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"Outline HTTP error: {exc}") from exc
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd vts-outline && uv run pytest tests/ -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Install into the image**

In `Dockerfile`, after the main app install, add (match the project's build layering):

```dockerfile
COPY vts-outline /app/vts-outline
RUN pip install --no-deps /app/vts-outline
```
(Use `--no-deps` because `vts` + `httpx` are already installed; adjust to the project's install tooling — uv/pip.)

- [ ] **Step 7: Verify discovery end-to-end**

Run (with `vts-outline` installed into the same env): `uv run python -c "from vts.delivery.registry import list_adapters; print(list(list_adapters()))"`
Expected: output includes `'outline'`.

- [ ] **Step 8: Commit**

```bash
git add vts-outline Dockerfile
git commit -m "feat(vts-outline): Outline delivery adapter package + image install (vts-ouq)"
```

---

## Task 14: End-to-end wiring test + docs + final gates

**Files:**
- Create: `tests/test_delivery_e2e.py`
- Modify: `.env.example` (new env vars), `vts/__init__.py` (version bump), README/docs note if the project documents MCP tools.

**Interfaces:** none new — this task proves the pieces compose and documents operator-facing config.

- [ ] **Step 1: Write an end-to-end test with a fake adapter**

```python
# tests/test_delivery_e2e.py
import uuid
from datetime import datetime, timezone
import pytest
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult
from vts.delivery.queue import enqueue_deliveries
from vts.delivery.consumer import delivery_tick
from vts.db.repo import Repo
from vts.db.models import User, Task, TaskStatus, DeliveryStatus


class RecordingAdapter:
    name = "rec"
    def __init__(self): self.calls = []
    def config_schema(self): return {}
    def secret_keys(self): return ["api_token"]
    async def deliver(self, payload, target):
        self.calls.append((payload.variant, target.secrets.get("api_token")))
        return DeliveryResult(external_id="e1", external_url="http://o/e1")


@pytest.mark.asyncio
async def test_enqueue_then_consume(db_session, tmp_path, monkeypatch, session_factory):
    from vts.core.config import get_settings
    from cryptography.fernet import Fernet
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_key", Fernet.generate_key().decode(), raising=False)
    rec = RecordingAdapter()
    monkeypatch.setattr(registry, "_CACHE", {"rec": rec}, raising=False)

    repo = Repo(db_session)
    u = User(username=f"u-{uuid.uuid4().hex[:8]}"); db_session.add(u); await db_session.flush()
    from vts.core.secrets import encrypt_secrets
    await repo.create_delivery_target(
        u.id, name="out", adapter="rec", config={"default_variant": "raw"},
        secrets_enc=encrypt_secrets({"api_token": "tok"}, settings.secrets_key))
    p = tmp_path / "transcript.txt"; p.write_text("hi")
    t = Task(user_id=u.id, source_url="http://x",
             options={"delivery": [{"deliver_to": "out"}]},
             artifact_dir=str(tmp_path), transcript_path=str(p),
             status=TaskStatus.completed)
    db_session.add(t); await db_session.flush()

    n = await enqueue_deliveries(repo, t, max_attempts=3, now=datetime.now(timezone.utc))
    await db_session.commit()
    assert n == 1

    await delivery_tick(session_factory, settings, datetime.now(timezone.utc))
    rows = await repo.list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rec.calls == [("raw", "tok")]  # secret decrypted and passed to adapter
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_delivery_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Document env vars**

Add to `.env.example`:

```bash
# Delivery: Fernet key for encrypting delivery-target secrets at rest.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
VTS_SECRETS_KEY=
# Delivery retry tuning (optional; defaults shown)
DELIVERY_MAX_ATTEMPTS=5
DELIVERY_BACKOFF_BASE_SECONDS=60
DELIVERY_BACKOFF_CAP_SECONDS=3600
DELIVERY_STUCK_SECONDS=600
```

(Match the project's env-var → Settings naming convention; if it uses a prefix/alias map like the `services_*` entries seen in config, add the aliases there too.)

- [ ] **Step 4: Run the full delivery suite + broader gates**

Run: `uv run pytest tests/delivery tests/test_delivery_*.py tests/mcp/test_tools_delivery.py -v && uv run ruff check vts/delivery vts/core/secrets.py`
Expected: all PASS / clean.

- [ ] **Step 5: Bump version and commit**

Bump `vts/__init__.py`.

```bash
git add tests/test_delivery_e2e.py .env.example vts/__init__.py
git commit -m "test(delivery): e2e enqueue→consume; document VTS_SECRETS_KEY + tuning (vts-ouq)"
```

---

## Self-Review

**Spec coverage:**

| Spec item | Task |
|---|---|
| Contract (`DeliveryAdapter`, `DeliveryPayload`, ...) | Task 2 |
| entry_points discovery | Task 3 |
| DeliveryTarget (encrypted secrets, user-scoped) | Task 1 (encryption), 4 (model), 5 (repo), 10 (REST), 12 (MCP) |
| DeliveryAttempt + durable queue | Task 4 (model), 6 (repo) |
| enqueue at completion (deliver_safe) | Task 8 |
| consumer loop + backoff + dead + reaper + SKIP LOCKED | Task 6 (claim), 9 (loop) |
| variant → content resolution | Task 7 |
| REST CRUD + status + retry, write-only secrets | Task 10, 11 |
| MCP CRUD + submit.delivery + status/retry | Task 12 |
| delivery in presets + allowlist trap | Task 12 |
| submit replaces preset delivery | Task 12 |
| Outline adapter (separate package, idempotent) | Task 13 |
| secrets never exposed | Tasks 10, 11, 12 (presence markers), 14 (e2e asserts decrypt only to adapter) |
| VTS_SECRETS_KEY fail-loud | Task 1 (`SecretsKeyMissing`), 9 (consumer surfaces it as a retryable failure) |
| e2e proof | Task 14 |

Deferred items (SSE `delivery_status`, targets UI, webhook/S3 adapters, multi-worker load) are intentionally **not** tasks — matches the spec's "Отложено" list.

**Placeholder scan:** No "TBD"/"add error handling"-style placeholders. Two spots say "match the project's existing X" (session fixture name, redis access pattern in `main.py`, Dockerfile install tooling) — these are deliberate pointers to verify a real existing convention at implementation time, not missing content; each is accompanied by concrete code.

**Type consistency:** Method names are consistent across tasks: `create_delivery_target`, `get_delivery_target_by_name`, `create_delivery_attempt`, `claim_due_deliveries`, `record_delivery_result`, `record_delivery_failure`, `reap_stuck_deliveries`, `reset_delivery_for_retry`, `enqueue_deliveries`, `resolve_variant`, `get_adapter`, `backoff_seconds`, `process_one_delivery`/`delivery_tick`/`delivery_loop`. Schema names consistent: `DeliveryTargetCreate/Update/Out`, `DeliveryOut`. `_MARKER_PREFIX`/`vts-task:` used consistently in Task 13.

**One implementation note flagged for the executor:** Task 9's `process_one_delivery` uses an inline `__import__` only to make the import explicit in-plan; replace with a top-level `from vts.db.models import DeliveryAttempt`. Called out in the task text.
