# Speaker Registry & Voice Enrollment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give diarized speakers persistent names by matching their voice against a per-user registry, pausing the pipeline for manual review when matches are uncertain.

**Architecture:** Three new tables (`Speaker`, `VoiceSample`, `MatchDecision`) on pgvector, a `match_speakers` pipeline step that runs after `diarize`, a new `awaiting_input` task status that releases the worker while a human resolves ambiguous voices, and two web dialogs (registry management + per-task voice resolution). The diarization sidecar gains an `/embed` endpoint and reports its model id. Matching compares each task's cluster embedding against registry fragment embeddings and picks the nearest.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, Alembic, pgvector (`Vector` type from `pgvector.sqlalchemy`), Postgres via `VTS_TEST_DATABASE_URL`, pyannote sidecar (FastAPI + torch), vanilla JS frontend (`vts/static/app.js`).

## Global Constraints

- **Embedding dimension is 256**, measured on live `/diarize` 2026-07-17 (pyannote community-1 / WeSpeaker ResNet, NOT ECAPA 192). `Vector(256)`, fixed.
- **Vectors are unnormalised** (L2 1.96–3.68). Use cosine distance (`<=>`) only; never L2 (`<->`). No published threshold applies — calibrate from `MatchDecision` data.
- **Every embedding and every distance carries `embedding_model`.** Matching and calibration filter on it. A vector without provenance is meaningless.
- **`Speaker` is per-user**, isolated by `.where(...user_id == user_id)`, mirroring `Prompt` (`vts/db/repo.py:460`). No system speakers.
- **Repo methods `flush()`, never `commit()`** — the caller (endpoint / step) owns the transaction.
- **Forbidden actions are made technically impossible** (hidden/disabled control); permitted actions with side effects show an exhaustive confirmation. (bd memory `ui-forbidden-actions-pattern`.)
- **Audio stored in DB as `bytea`, `deferred=True`** — never loaded during matching.
- **`vts/static/app.js` loads without `defer`** — new DOM blocks referenced via `getElementById` must precede the `<script>` tag in `index.html`.
- **Bump `vts/__init__.py` version before each commit that ships behavior.**
- Thresholds live in config (`Settings`), never hardcoded: `speaker_match_max_distance_auto`, `speaker_match_max_distance_candidate`, `speaker_preview_count`, `speaker_preview_seconds`, `speaker_preview_min_segment`.

**Spec:** `docs/superpowers/specs/2026-07-17-speaker-registry-design.md`

---

## File Structure

**New files:**
- `vts/services/speaker_registry.py` — matching logic (bucket a distance into auto/grey/miss), pure functions over distances + thresholds.
- `vts/pipeline/steps/speaker_match.py` — `MatchSpeakersStep`.
- `tests/test_speaker_registry_repo.py`, `tests/test_speaker_match_service.py`, `tests/test_speaker_match_step.py`, `tests/test_speaker_api.py`, `tests/test_match_decision.py`.
- `alembic/versions/0014_pgvector_extension.py`, `0015_speakers.py`, `0016_voice_samples.py`, `0017_match_decisions.py`, `0018_task_status_awaiting_input.py`.

**Modified files:**
- `.github/workflows/tests.yml` — Postgres image → pgvector-carrying image.
- `docker-compose.yml` — Postgres image → `tensorchord/vchord-postgres:pg17-v1.1.1`.
- `tests/conftest.py` / `tests/_db.py` — create `vector` extension before `create_all`.
- `vts/db/models.py` — `Speaker`, `VoiceSample`, `MatchDecision`, `TaskStatus.awaiting_input`, `Task.awaiting_step`.
- `vts/db/repo.py` — registry + match-decision CRUD.
- `vts/services/task_status.py` — predicates for `awaiting_input`.
- `vts/core/config.py` — five settings + env aliases.
- `docker/diarization/server.py` — `/embed`, model id in responses.
- `vts/services/diarization/_base.py` — client `embed()` + model id passthrough.
- `vts/pipeline/steps/diarization.py` — cut preview fragments.
- `vts/pipeline/types.py` (DAG wiring) + `vts/pipeline/steps/registry.py` — register `match_speakers`.
- `vts/api/main.py` — registry CRUD endpoints, voice-resolution endpoint, sample audio endpoint.
- `vts/static/index.html`, `vts/static/app.js`, `vts/static/styles.css` — two dialogs + "Доработать" button + create-form checkbox.

---

## Task 1: Test database carries pgvector

**Files:**
- Modify: `.github/workflows/tests.yml:19`
- Modify: `docker-compose.yml:68`
- Modify: `tests/_db.py`
- Modify: `tests/conftest.py:79-81`

**Interfaces:**
- Produces: a test/dev Postgres where `CREATE EXTENSION vector` works and `Vector(256)` columns can be created via `create_all`.

- [ ] **Step 1: Point compose and CI at the VectorChord image**

In `docker-compose.yml`, change the postgres service image:
```yaml
  postgres:
    image: docker.io/tensorchord/vchord-postgres:pg17-v1.1.1
```

In `.github/workflows/tests.yml`, change the services.postgres image:
```yaml
      postgres:
        image: tensorchord/vchord-postgres:pg17-v1.1.1
```

Why this exact image, not `pgvector/pgvector`: prod (`beelink.fritz.box`) runs it, and VectorChord ships an extra `vchord` extension a plain pgvector image lacks — a silent dev/prod split otherwise (bd memory `test_environment_parity`).

- [ ] **Step 2: Create the extension before schema build in the shared engine helper**

In `tests/_db.py`, add a helper that ensures the extension, and call it wherever the schema is built. Add:
```python
from sqlalchemy import text


async def ensure_pgvector(engine) -> None:
    """CREATE EXTENSION vector before create_all — Vector columns need it, and
    tests build the schema with create_all rather than running migrations."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
```

- [ ] **Step 3: Call ensure_pgvector in conftest before create_all**

In `tests/conftest.py`, modify the `authed_app` fixture so the extension exists before `create_all`:
```python
    from tests._db import ensure_pgvector

    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
```
(Import path: if `_db` is imported as top-level `from _db import make_test_engine`, keep the same style — `from _db import make_test_engine, ensure_pgvector`.)

- [ ] **Step 4: Write a smoke test proving the extension is available**

Create `tests/test_pgvector_available.py`:
```python
import pytest
from sqlalchemy import text

from _db import make_test_engine, ensure_pgvector


@pytest.mark.asyncio
async def test_vector_extension_present():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        row = await conn.execute(
            text("SELECT installed_version FROM pg_available_extensions WHERE name='vector'")
        )
        assert row.scalar() is not None
    await engine.dispose()
```

- [ ] **Step 5: Run the smoke test**

Run: `pytest tests/test_pgvector_available.py -v`
Expected: PASS (requires the test Postgres to be the VectorChord image; if running locally, `docker compose up -d postgres` first with the new image).

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .github/workflows/tests.yml tests/_db.py tests/conftest.py tests/test_pgvector_available.py
git commit -m "test(db): run tests on the VectorChord image, enable pgvector (vts-80i)"
```

---

## Task 2: Speaker + VoiceSample models and migrations

**Files:**
- Modify: `vts/db/models.py` (after the `Preset` model)
- Create: `alembic/versions/0014_pgvector_extension.py`
- Create: `alembic/versions/0015_speakers.py`
- Create: `alembic/versions/0016_voice_samples.py`
- Test: `tests/test_speaker_registry_repo.py`

**Interfaces:**
- Produces:
  - `Speaker(id: UUID, user_id: UUID, name: str, created_at, updated_at)`
  - `VoiceSample(id: UUID, speaker_id: UUID, embedding: list[float] len 256, embedding_model: str, audio: bytes, audio_format: str, duration_sec: float, source_task_id: UUID|None, created_at)`
  - `VoiceSample.audio` is `deferred=True`.

- [ ] **Step 1: Write the failing model test**

Create `tests/test_speaker_registry_repo.py`:
```python
import uuid
import pytest
from sqlalchemy import select

from vts.db.models import Speaker, VoiceSample
from tests._db import make_test_engine, ensure_pgvector
from vts.db.base import Base
from vts.db.models import User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_speaker_and_sample_roundtrip(factory):
    async with factory() as s:
        sp = Speaker(user_id=_USER, name="Вася")
        s.add(sp)
        await s.flush()
        vs = VoiceSample(
            speaker_id=sp.id,
            embedding=[0.1] * 256,
            embedding_model="wespeaker-resnet34-256",
            audio=b"RIFF....",
            audio_format="wav",
            duration_sec=5.0,
            source_task_id=None,
        )
        s.add(vs)
        await s.commit()
    async with factory() as s:
        got = await s.scalar(select(VoiceSample).where(VoiceSample.speaker_id == sp.id))
        assert got is not None
        assert len(got.embedding) == 256
        assert got.embedding_model == "wespeaker-resnet34-256"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `pytest tests/test_speaker_registry_repo.py::test_speaker_and_sample_roundtrip -v`
Expected: FAIL with `ImportError: cannot import name 'Speaker'`.

- [ ] **Step 3: Add the models**

In `vts/db/models.py`, add the pgvector import near the top:
```python
from pgvector.sqlalchemy import Vector
```
After the `Preset` model, add (match existing column style — `Mapped`/`mapped_column`, `utcnow` default already defined in the file):
```python
class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VoiceSample(Base):
    __tablename__ = "voice_samples"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    speaker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speakers.id", ondelete="CASCADE"), index=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(256))
    embedding_model: Mapped[str] = mapped_column(String)
    audio: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    audio_format: Mapped[str] = mapped_column(String, default="wav")
    duration_sec: Mapped[float] = mapped_column(Float)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```
Ensure `LargeBinary`, `Float`, `String`, `ForeignKey`, `deferred` are imported at the top of the file (check the existing import block; add whichever are missing — `deferred` comes from `sqlalchemy.orm`).

- [ ] **Step 4: Run the test, verify it passes**

Run: `pytest tests/test_speaker_registry_repo.py::test_speaker_and_sample_roundtrip -v`
Expected: PASS.

- [ ] **Step 5: Write the extension migration**

Create `alembic/versions/0014_pgvector_extension.py`:
```python
"""Enable pgvector."""
from __future__ import annotations
from alembic import op

revision = "0014_pgvector_extension"
down_revision = "0013_task_status_waiting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
```

- [ ] **Step 6: Write the speakers + voice_samples migrations**

Create `alembic/versions/0015_speakers.py`:
```python
"""Speaker registry."""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = "0015_speakers"
down_revision = "0014_pgvector_extension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speakers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("speakers")
```

Create `alembic/versions/0016_voice_samples.py`:
```python
"""Voice samples with pgvector embeddings."""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0016_voice_samples"
down_revision = "0015_speakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_samples",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("speaker_id", sa.Uuid(), sa.ForeignKey("speakers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("embedding", Vector(256), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("audio", sa.LargeBinary(), nullable=False),
        sa.Column("audio_format", sa.String(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("voice_samples")
```

- [ ] **Step 7: Verify migrations apply cleanly**

Run: `alembic upgrade head` against a scratch DB (or `alembic upgrade head` then `alembic downgrade 0013_task_status_waiting`).
Expected: no error; `speakers` and `voice_samples` tables exist after upgrade, gone after downgrade.

- [ ] **Step 8: Commit**

```bash
git add vts/db/models.py alembic/versions/0014_pgvector_extension.py alembic/versions/0015_speakers.py alembic/versions/0016_voice_samples.py tests/test_speaker_registry_repo.py
git commit -m "feat(db): Speaker + VoiceSample models, pgvector migrations (vts-80i)"
```

---

## Task 3: Registry CRUD repo methods + cascade test

**Files:**
- Modify: `vts/db/repo.py` (after Preset CRUD block, ~line 540)
- Test: `tests/test_speaker_registry_repo.py`

**Interfaces:**
- Consumes: `Speaker`, `VoiceSample` from Task 2.
- Produces:
  - `create_speaker(user_id, name) -> Speaker`
  - `list_speakers(user_id) -> list[Speaker]`
  - `get_speaker(user_id, speaker_id) -> Speaker | None`
  - `rename_speaker(user_id, speaker_id, name) -> Speaker | None`
  - `delete_speaker(user_id, speaker_id) -> bool`
  - `add_voice_sample(speaker_id, embedding, embedding_model, audio, audio_format, duration_sec, source_task_id) -> VoiceSample`
  - `list_voice_samples(speaker_id) -> list[VoiceSample]` (audio NOT loaded)
  - `get_voice_sample(user_id, sample_id) -> VoiceSample | None` (joins speaker for user isolation)
  - `delete_voice_sample(user_id, sample_id) -> bool`
  - `load_sample_audio(user_id, sample_id) -> tuple[bytes, str] | None` (audio + format, undeferred)

- [ ] **Step 1: Write failing tests for CRUD + cascade + user isolation**

Append to `tests/test_speaker_registry_repo.py`:
```python
from vts.db.repo import Repo


@pytest.mark.asyncio
async def test_speaker_crud_and_isolation(factory):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    async with factory() as s:
        s.add(User(id=other, username="other"))
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        await s.commit()
        assert (await repo.get_speaker(other, sp.id)) is None  # isolation
        rows = await repo.list_speakers(_USER)
        assert [r.name for r in rows] == ["Вася"]
        renamed = await repo.rename_speaker(_USER, sp.id, "Василий")
        assert renamed.name == "Василий"
        assert await repo.delete_speaker(_USER, sp.id) is True
        assert await repo.list_speakers(_USER) == []


@pytest.mark.asyncio
async def test_delete_speaker_cascades_samples(factory):
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256,
            embedding_model="m", audio=b"x", audio_format="wav",
            duration_sec=5.0, source_task_id=None,
        )
        await s.commit()
        assert len(await repo.list_voice_samples(sp.id)) == 1
        await repo.delete_speaker(_USER, sp.id)
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        assert await repo.list_voice_samples(sp.id) == []


@pytest.mark.asyncio
async def test_load_sample_audio_and_delete(factory):
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        vs = await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256,
            embedding_model="m", audio=b"AUDIOBYTES", audio_format="wav",
            duration_sec=5.0, source_task_id=None,
        )
        await s.commit()
        audio, fmt = await repo.load_sample_audio(_USER, vs.id)
        assert audio == b"AUDIOBYTES" and fmt == "wav"
        assert await repo.delete_voice_sample(_USER, vs.id) is True
        assert await repo.load_sample_audio(_USER, vs.id) is None
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_registry_repo.py -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'create_speaker'`.

- [ ] **Step 3: Implement the repo methods**

In `vts/db/repo.py`, after the Preset CRUD block, add (mirror the Prompt style — `flush()`, user_id in the where clause; import `Speaker`, `VoiceSample`, `undefer` from sqlalchemy.orm):
```python
    # ------------------------------------------------------------------
    # Speaker registry CRUD
    # ------------------------------------------------------------------

    async def create_speaker(self, user_id: uuid.UUID, name: str) -> Speaker:
        speaker = Speaker(user_id=user_id, name=name)
        self.session.add(speaker)
        await self.session.flush()
        return speaker

    async def list_speakers(self, user_id: uuid.UUID) -> list[Speaker]:
        stmt = select(Speaker).where(Speaker.user_id == user_id).order_by(Speaker.name.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID) -> Speaker | None:
        stmt = select(Speaker).where(Speaker.id == speaker_id, Speaker.user_id == user_id)
        return await self.session.scalar(stmt)

    async def rename_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID, name: str) -> Speaker | None:
        speaker = await self.get_speaker(user_id, speaker_id)
        if speaker is None:
            return None
        speaker.name = name
        await self.session.flush()
        return speaker

    async def delete_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID) -> bool:
        speaker = await self.get_speaker(user_id, speaker_id)
        if speaker is None:
            return False
        await self.session.delete(speaker)
        await self.session.flush()
        return True

    async def add_voice_sample(
        self, *, speaker_id: uuid.UUID, embedding: list[float], embedding_model: str,
        audio: bytes, audio_format: str, duration_sec: float,
        source_task_id: uuid.UUID | None,
    ) -> VoiceSample:
        sample = VoiceSample(
            speaker_id=speaker_id, embedding=embedding, embedding_model=embedding_model,
            audio=audio, audio_format=audio_format, duration_sec=duration_sec,
            source_task_id=source_task_id,
        )
        self.session.add(sample)
        await self.session.flush()
        return sample

    async def list_voice_samples(self, speaker_id: uuid.UUID) -> list[VoiceSample]:
        # audio stays deferred — never loaded here
        stmt = (
            select(VoiceSample)
            .where(VoiceSample.speaker_id == speaker_id)
            .order_by(VoiceSample.created_at.asc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_voice_sample(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> VoiceSample | None:
        stmt = (
            select(VoiceSample)
            .join(Speaker, VoiceSample.speaker_id == Speaker.id)
            .where(VoiceSample.id == sample_id, Speaker.user_id == user_id)
        )
        return await self.session.scalar(stmt)

    async def delete_voice_sample(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> bool:
        sample = await self.get_voice_sample(user_id, sample_id)
        if sample is None:
            return False
        await self.session.delete(sample)
        await self.session.flush()
        return True

    async def load_sample_audio(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> tuple[bytes, str] | None:
        stmt = (
            select(VoiceSample)
            .join(Speaker, VoiceSample.speaker_id == Speaker.id)
            .where(VoiceSample.id == sample_id, Speaker.user_id == user_id)
            .options(undefer(VoiceSample.audio))
        )
        sample = await self.session.scalar(stmt)
        if sample is None:
            return None
        return sample.audio, sample.audio_format
```

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_speaker_registry_repo.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_speaker_registry_repo.py
git commit -m "feat(db): speaker + voice-sample registry CRUD (vts-80i)"
```

---

## Task 4: Matching service (distance → bucket)

**Files:**
- Create: `vts/services/speaker_registry.py`
- Modify: `vts/core/config.py`
- Test: `tests/test_speaker_match_service.py`

**Interfaces:**
- Produces:
  - `class MatchOutcome(StrEnum)`: `auto`, `grey`, `miss`
  - `bucket(distance: float | None, auto: float, candidate: float) -> MatchOutcome` — `None` (no candidate at all) → `miss`; `<= auto` → `auto`; `<= candidate` → `grey`; else `miss`.
  - Repo method `nearest_speakers(user_id, embedding, embedding_model, limit=None) -> list[tuple[Speaker, float]]` — all user speakers with a sample in the given model, ranked by `MIN(embedding <=> query)` ascending. `limit=None` = all.

- [ ] **Step 1: Write failing service test**

Create `tests/test_speaker_match_service.py`:
```python
import pytest
from vts.services.speaker_registry import bucket, MatchOutcome


@pytest.mark.parametrize("dist,expected", [
    (None, MatchOutcome.miss),
    (0.10, MatchOutcome.auto),
    (0.30, MatchOutcome.auto),   # == auto boundary is auto
    (0.31, MatchOutcome.grey),
    (0.60, MatchOutcome.grey),   # == candidate boundary is grey
    (0.61, MatchOutcome.miss),
])
def test_bucket(dist, expected):
    assert bucket(dist, auto=0.30, candidate=0.60) == expected
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_match_service.py -v`
Expected: FAIL — `ModuleNotFoundError: vts.services.speaker_registry`.

- [ ] **Step 3: Implement bucket + config settings**

Create `vts/services/speaker_registry.py`:
```python
"""Speaker-matching decisions over cosine distances.

Distances come from pgvector `<=>` (cosine): smaller = more similar. Thresholds
are in the same unit, so `auto` is numerically SMALLER than `candidate`.
Vectors are unnormalised (measured 2026-07-17), so cosine is the only sane
operator — never L2.
"""
from __future__ import annotations

from enum import StrEnum


class MatchOutcome(StrEnum):
    auto = "auto"
    grey = "grey"
    miss = "miss"


def bucket(distance: float | None, *, auto: float, candidate: float) -> MatchOutcome:
    """Classify a nearest-fragment distance into the three UX outcomes.

    None means no candidate existed at all (empty registry / no sample in this
    model) — a miss, not an error.
    """
    if distance is None:
        return MatchOutcome.miss
    if distance <= auto:
        return MatchOutcome.auto
    if distance <= candidate:
        return MatchOutcome.grey
    return MatchOutcome.miss
```

In `vts/core/config.py`, add near the other diarization settings (line ~103):
```python
    speaker_match_max_distance_auto: float = 0.25      # <= -> auto-bind (conservative start)
    speaker_match_max_distance_candidate: float = 0.55  # > -> not even a candidate
    speaker_preview_count: int = 3
    speaker_preview_seconds: float = 5.0
    speaker_preview_min_segment: float = 2.0
```
And in the env-alias map (line ~414 area, alongside `services_diarization_*`):
```python
        "services_speaker_match_max_distance_auto": "speaker_match_max_distance_auto",
        "services_speaker_match_max_distance_candidate": "speaker_match_max_distance_candidate",
        "services_speaker_preview_count": "speaker_preview_count",
        "services_speaker_preview_seconds": "speaker_preview_seconds",
        "services_speaker_preview_min_segment": "speaker_preview_min_segment",
```
(Starting thresholds are placeholders pending calibration — see spec. They live in config precisely so they move without a rebuild.)

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_speaker_match_service.py -v`
Expected: PASS.

- [ ] **Step 5: Write failing test for nearest_speakers (real pgvector query)**

Append to `tests/test_speaker_match_service.py`:
```python
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo
from _db import make_test_engine, ensure_pgvector

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_nearest_by_min_distance_and_model_filter(factory):
    async with factory() as s:
        repo = Repo(s)
        vasya = await repo.create_speaker(_USER, "Вася")
        petya = await repo.create_speaker(_USER, "Петя")
        # Vasya has two samples; the nearer one must decide his rank (MIN).
        await repo.add_voice_sample(speaker_id=vasya.id, embedding=[1.0] + [0.0]*255,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await repo.add_voice_sample(speaker_id=vasya.id, embedding=[0.9, 0.1] + [0.0]*254,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await repo.add_voice_sample(speaker_id=petya.id, embedding=[0.0, 1.0] + [0.0]*254,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        # A sample from a different model must be excluded.
        await repo.add_voice_sample(speaker_id=petya.id, embedding=[1.0] + [0.0]*255,
            embedding_model="OTHER", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await s.commit()

        ranked = await repo.nearest_speakers(_USER, [1.0] + [0.0]*255, "m1")
        # Vasya first (has an identical-direction sample), Petya second.
        assert [sp.name for sp, _ in ranked] == ["Вася", "Петя"]
        # Petya's distance must come from his m1 sample, not the OTHER one.
        petya_dist = next(d for sp, d in ranked if sp.name == "Петя")
        assert petya_dist > 0.5
```

- [ ] **Step 6: Run, verify failure**

Run: `pytest tests/test_speaker_match_service.py::test_nearest_by_min_distance_and_model_filter -v`
Expected: FAIL — `AttributeError: ... 'nearest_speakers'`.

- [ ] **Step 7: Implement nearest_speakers in repo**

In `vts/db/repo.py`, add to the registry block:
```python
    async def nearest_speakers(
        self, user_id: uuid.UUID, embedding: list[float], embedding_model: str,
        limit: int | None = None,
    ) -> list[tuple[Speaker, float]]:
        """User's speakers ranked by their nearest fragment (MIN cosine distance).

        Only samples computed by `embedding_model` count: distances across models
        are meaningless. `<=>` is cosine — smaller is nearer.
        """
        dist = func.min(VoiceSample.embedding.cosine_distance(embedding)).label("dist")
        stmt = (
            select(Speaker, dist)
            .join(VoiceSample, VoiceSample.speaker_id == Speaker.id)
            .where(Speaker.user_id == user_id, VoiceSample.embedding_model == embedding_model)
            .group_by(Speaker.id)
            .order_by(dist.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self.session.execute(stmt)
        return [(row[0], float(row[1])) for row in rows.all()]
```
Ensure `func` is imported from sqlalchemy at the top of `repo.py`.

- [ ] **Step 8: Run, verify passes**

Run: `pytest tests/test_speaker_match_service.py -v`
Expected: PASS (both tests). `cosine_distance` is pgvector's operator helper.

- [ ] **Step 9: Commit**

```bash
git add vts/services/speaker_registry.py vts/core/config.py vts/db/repo.py tests/test_speaker_match_service.py
git commit -m "feat(speakers): nearest-fragment matching + distance bucketing (vts-80i)"
```

---

## Task 5: Sidecar /embed endpoint + model identity

**Files:**
- Modify: `docker/diarization/server.py`
- Test: `docker/diarization/test_server.py` (create if absent; otherwise co-locate)

**Interfaces:**
- Produces:
  - `POST /embed` — multipart `file` (wav) → `{"embedding": [float×256], "embedding_model": str}`.
  - `/diarize` response gains `"embedding_model": str`.
  - `/health` gains `"embedding_model": str`.
  - Module constant `EMBEDDING_MODEL_ID` (e.g. `"pyannote-community-1/wespeaker-resnet34-256"`) — the single source the app reads.

- [ ] **Step 1: Write failing test for /embed shape**

Create `docker/diarization/test_server.py`:
```python
"""Sidecar contract tests. Model loading is mocked — we assert wire shape only."""
from fastapi.testclient import TestClient
import server


def test_health_reports_model(monkeypatch):
    client = TestClient(server.app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["embedding_model"] == server.EMBEDDING_MODEL_ID


def test_embed_returns_vector(monkeypatch):
    # Stub the embedding model so no weights are loaded.
    monkeypatch.setattr(server, "_embed_wav", lambda path: [0.5] * 256)
    client = TestClient(server.app)
    r = client.post("/embed", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
    assert r.status_code == 200
    body = r.json()
    assert len(body["embedding"]) == 256
    assert body["embedding_model"] == server.EMBEDDING_MODEL_ID
```

- [ ] **Step 2: Run, verify failure**

Run: `cd docker/diarization && python -m pytest test_server.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'EMBEDDING_MODEL_ID'`.

- [ ] **Step 3: Add model id, /embed, and thread it through responses**

In `docker/diarization/server.py`:

Add the constant near the top:
```python
EMBEDDING_MODEL_ID = "pyannote-community-1/wespeaker-resnet34-256"
```

Add an embedding helper that reuses the pipeline's embedding model (the weights are already in the image at `embedding/pytorch_model.bin`):
```python
def _embed_wav(audio_path: str) -> list[float]:
    """Embed a single wav clip with the pipeline's embedding model.

    Reuses the model already loaded for diarization; no extra weights.
    """
    from pyannote.audio import Model
    import torchaudio

    model = _embedding_model()  # lazy singleton, see below
    waveform, sample_rate = torchaudio.load(audio_path)
    with torch.no_grad():
        emb = model(waveform.unsqueeze(0) if waveform.dim() == 2 else waveform)
    return [float(x) for x in emb.reshape(-1).tolist()]
```
Add a lazy singleton for the embedding model, mirroring `pipeline()`:
```python
_embedding: "Model | None" = None


def _embedding_model():
    global _embedding
    if _embedding is None:
        from pyannote.audio import Model
        model_dir = os.environ.get("MODEL_DIR", "/models")
        _embedding = Model.from_pretrained(Path(model_dir) / "embedding" / "pytorch_model.bin")
        _embedding.to(torch.device(os.environ.get("TORCH_DEVICE", "cpu")))
        _embedding.eval()
    return _embedding
```
(NOTE for implementer: verify the exact pyannote 4.x load call for the standalone embedding model during Task 12's live run — `Model.from_pretrained` path may differ. The test mocks `_embed_wav`, so unit tests pass regardless; the real call is validated live.)

Add the endpoint:
```python
@app.post("/embed")
async def embed(file: UploadFile = File(...)) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(await file.read())
        audio_path = Path(handle.name)
    try:
        vector = _embed_wav(str(audio_path))
    except Exception as error:  # noqa: BLE001
        _log.exception("embedding failed")
        raise HTTPException(status_code=500, detail=f"embedding failed: {error}") from error
    finally:
        audio_path.unlink(missing_ok=True)
    return {"embedding": vector, "embedding_model": EMBEDDING_MODEL_ID}
```
Update `/health`:
```python
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "embedding_model": EMBEDDING_MODEL_ID}
```
Update the `/diarize` return to include the model id:
```python
    return {"segments": segments, "embeddings": embeddings,
            "num_speakers": len(labels), "embedding_model": EMBEDDING_MODEL_ID}
```

- [ ] **Step 4: Run, verify passes**

Run: `cd docker/diarization && python -m pytest test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Bump the sidecar version**

In `docker/diarization/VERSION`, bump minor (e.g. `1.0.1` → `1.1.0`): `/embed` is a new capability.

- [ ] **Step 6: Commit**

```bash
git add docker/diarization/server.py docker/diarization/test_server.py docker/diarization/VERSION
git commit -m "feat(diarization): /embed endpoint and model identity (vts-80i)"
```

---

## Task 6: Diarization client — embed() + model passthrough

**Files:**
- Modify: `vts/services/diarization/_base.py`
- Test: `tests/test_diarization_backends.py`

**Interfaces:**
- Consumes: sidecar `/embed`, `embedding_model` field.
- Produces:
  - `DiarizationBackend.embed(audio_path: Path) -> list[float]`
  - `diarize(...)` result dict includes `embedding_model` (passed through from the sidecar).

- [ ] **Step 1: Write failing test (stub HTTP)**

Add to `tests/test_diarization_backends.py` a test that the client posts to `/embed` and returns the vector, and that `diarize` passes `embedding_model` through. Follow the existing stub style in that file (it already stubs the sidecar HTTP). Concretely:
```python
@pytest.mark.asyncio
async def test_embed_posts_and_returns_vector(monkeypatch):
    # Reuse the file's existing httpx stubbing pattern; the response body:
    stub_response = {"embedding": [0.25] * 256, "embedding_model": "m-test"}
    # ... wire stub so POST /embed returns stub_response ...
    backend = create_diarization_backend(_settings_with_url("http://diar:9100"))
    vec = await backend.embed(Path("/tmp/clip.wav"))
    assert len(vec) == 256
```
(Implementer: mirror the exact monkeypatch/stub already used by the diarize test in this file — do not invent a new HTTP mock.)

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_diarization_backends.py -k embed -v`
Expected: FAIL — no `embed` method.

- [ ] **Step 3: Implement embed() and passthrough**

In `vts/services/diarization/_base.py`, add to the backend (mirror the existing `diarize` httpx call):
```python
    async def embed(self, audio_path: Path) -> list[float]:
        with audio_path.open("rb") as fh:
            files = {"file": (audio_path.name, fh, "audio/wav")}
            resp = await self._client.post(f"{self._url}/embed", files=files, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        return [float(x) for x in data["embedding"]]
```
In the `diarize` method's return normalisation, carry `embedding_model` from the sidecar response into the returned dict (default to `""` if absent, so old sidecars degrade rather than crash).

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_diarization_backends.py -k embed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/services/diarization/_base.py tests/test_diarization_backends.py
git commit -m "feat(diarization): client embed() and model passthrough (vts-80i)"
```

---

## Task 6.5: Live validation — cluster and fragment embeddings share a space

**Files:**
- No production code. A throwaway script in `scratchpad/`, findings appended to `docs/superpowers/specs/2026-07-17-diarization-reference-run.json`.

**Why here, not at the end:** matching (Task 4), preview cutting (Task 7), and the
gate (Task 10) all rest on one unverified premise — that a fragment embedding from
`/embed` and a cluster embedding from `/diarize` live in the same space, so their
cosine distance is meaningful. If it does NOT hold, that is a design-level break in
Tasks 4/7/10, and it is far cheaper to learn it now (with `/embed` just built) than
after building matching on top. This task GATES Task 7 onward. Dims (256) are already
confirmed; this is specifically the shared-space check.

**Interfaces:**
- Consumes: live sidecar `http://ai-node1:9100` (`/diarize`, `/embed`), reference audio
  `/disk/vts-data/aad7edfee1ca2a5c20bff0dd/c31487fb-7667-4f92-8976-7b8de2b677ec/media/audio_16k_trimmed.wav`,
  the diarization client `embed()` from Task 6.

- [ ] **Step 1: Cut one clip from a known speaker's segment**

Using the reference `diarization.json` already produced in brainstorm (or re-run
`/diarize`), pick one speaker's longest segment, cut a 5s wav from its middle with
ffmpeg (`-ss <mid> -t 5 -ar 16000 -ac 1`).

- [ ] **Step 2: Embed the clip and compare against cluster vectors**

POST the clip to `/embed`. Then compute cosine distance from that fragment vector to
(a) the SAME speaker's cluster embedding in `diarization.json`, and (b) a DIFFERENT
speaker's cluster embedding.

```python
# scratchpad script: load diarization.json embeddings, POST clip to /embed,
# print cosine(fragment, same_speaker_cluster) and cosine(fragment, other_speaker_cluster)
def cosine(a, b):
    import math
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
    return 1 - dot / (na * nb)
```

- [ ] **Step 3: Assert the space holds**

Expected: same-speaker distance clearly SMALLER than different-speaker distance. If it
is not — STOP and escalate to the human. The hybrid matching premise (cluster-vs-fragment)
is broken and Tasks 4/7/10 need rethinking before proceeding. Do not paper over it with
threshold tuning — a systematic offset would masquerade as a threshold problem.

- [ ] **Step 4: Record findings and commit**

Append to `docs/superpowers/specs/2026-07-17-diarization-reference-run.json`: measured
model id from `/embed`, same/different distances, and a boolean `shared_space_holds`.

```bash
git add docs/superpowers/specs/2026-07-17-diarization-reference-run.json
git commit -m "docs(spec): confirm cluster/fragment embeddings share a space (vts-80i)"
```

---

## Task 7: DiarizeStep cuts preview fragments

**Files:**
- Modify: `vts/pipeline/steps/diarization.py`
- Test: `tests/test_diarization_step.py`

**Interfaces:**
- Consumes: `diarization.json` (segments per speaker), `ctx.settings.speaker_preview_*`.
- Produces: `outputs/speaker_previews.json` — `{speaker_label: [{"path": str, "start": float, "end": float}, ...]}`, wav clips under the task's outputs dir.

- [ ] **Step 1: Write failing test for fragment selection logic**

Add a pure-function test. First factor selection into a testable helper. Create the test in `tests/test_diarization_step.py`:
```python
from vts.pipeline.steps.diarization import select_preview_spans


def test_select_spans_spreads_across_segments():
    segments = [
        {"start": 0.0, "end": 30.0, "speaker": "S0"},   # long
        {"start": 40.0, "end": 45.0, "speaker": "S0"},  # 5s
        {"start": 50.0, "end": 51.0, "speaker": "S0"},  # too short (<2)
        {"start": 60.0, "end": 68.0, "speaker": "S0"},  # 8s
    ]
    spans = select_preview_spans(segments, "S0", count=3, clip_seconds=5.0, min_segment=2.0)
    # Three distinct source segments, longest first, each clip <= 5s, cut from inside.
    assert len(spans) == 3
    starts = [round(s["start"], 1) for s in spans]
    assert len(set(starts)) == 3  # distinct segments
    for s in spans:
        assert (s["end"] - s["start"]) <= 5.0 + 1e-6
        assert s["end"] - s["start"] >= 2.0  # nothing shorter than min survives as a clip


def test_select_spans_falls_back_to_one_segment_when_scarce():
    segments = [{"start": 0.0, "end": 30.0, "speaker": "S0"}]
    spans = select_preview_spans(segments, "S0", count=3, clip_seconds=5.0, min_segment=2.0)
    # Only one usable segment: take multiple non-overlapping clips from it.
    assert len(spans) == 3
    intervals = sorted((s["start"], s["end"]) for s in spans)
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert s2 >= e1  # non-overlapping
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_diarization_step.py -k select_spans -v`
Expected: FAIL — no `select_preview_spans`.

- [ ] **Step 3: Implement selection + wav cutting**

In `vts/pipeline/steps/diarization.py`, add the pure helper:
```python
def select_preview_spans(
    segments: list[dict], speaker: str, *, count: int, clip_seconds: float, min_segment: float,
) -> list[dict]:
    """Pick up to `count` clip spans of `clip_seconds` for one speaker.

    Prefers spreading clips across distinct segments (longest first), cutting each
    from the segment's middle. When usable segments run out, takes additional
    non-overlapping clips from the longest one so a monologue still yields variety.
    """
    own = [s for s in segments if str(s["speaker"]) == speaker
           and (float(s["end"]) - float(s["start"])) >= min_segment]
    own.sort(key=lambda s: float(s["end"]) - float(s["start"]), reverse=True)

    def middle_clip(seg: dict, offset: float = 0.0) -> dict:
        start, end = float(seg["start"]), float(seg["end"])
        length = end - start
        clip = min(clip_seconds, length)
        mid = start + (length - clip) / 2 + offset
        mid = max(start, min(mid, end - clip))
        return {"start": round(mid, 3), "end": round(mid + clip, 3)}

    spans: list[dict] = []
    for seg in own:
        if len(spans) >= count:
            break
        spans.append(middle_clip(seg))
    # Scarce case: refill from the longest segment with non-overlapping offsets.
    if len(spans) < count and own:
        longest = own[0]
        seg_len = float(longest["end"]) - float(longest["start"])
        step = clip_seconds
        offset = step
        while len(spans) < count and offset + clip_seconds <= seg_len:
            spans.append(middle_clip(longest, offset=offset - (seg_len - min(clip_seconds, seg_len)) / 2))
            offset += step
    return spans[:count]
```
Then in `DiarizeStep.run`, after `write_json(output, payload)`, cut the clips (use the existing audio path and an ffmpeg cut helper — the media service already has segment export; reuse `export_segments`-style slicing or a thin ffmpeg call). Write `speaker_previews.json`:
```python
        previews: dict[str, list[dict]] = {}
        audio_path = ctx.transcribe_audio_path(st.dirs)
        for label in {s["speaker"] for s in payload["segments"]}:
            spans = select_preview_spans(
                payload["segments"], str(label),
                count=ctx.settings.speaker_preview_count,
                clip_seconds=ctx.settings.speaker_preview_seconds,
                min_segment=ctx.settings.speaker_preview_min_segment,
            )
            clips = []
            for i, span in enumerate(spans):
                clip_path = st.dirs["outputs"] / f"preview_{label}_{i}.wav"
                await asyncio.to_thread(_cut_wav, audio_path, clip_path, span["start"], span["end"])
                clips.append({"path": str(clip_path), "start": span["start"], "end": span["end"]})
            previews[str(label)] = clips
        write_json(st.dirs["outputs"] / "speaker_previews.json", previews)
```
Add a small ffmpeg cut helper `_cut_wav(src, dst, start, end)` (mirror the ffmpeg invocation style in `vts/services/media.py`; `-ss <start> -to <end> -c copy` is unsafe for wav re-cut, use `-ss <start> -t <dur> -ar 16000 -ac 1`). Import `asyncio` if not present.

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_diarization_step.py -k select_spans -v`
Expected: PASS. (The wav-cutting path is exercised live in Task 12; unit tests cover span math.)

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/diarization.py tests/test_diarization_step.py
git commit -m "feat(diarization): cut representative preview fragments (vts-80i)"
```

---

## Task 8: awaiting_input task status

**Files:**
- Modify: `vts/db/models.py` (`TaskStatus`, add `Task.awaiting_step`)
- Modify: `vts/services/task_status.py`
- Create: `alembic/versions/0018_task_status_awaiting_input.py`
- Test: `tests/test_task_status_predicates.py` (existing file for predicates)

**Interfaces:**
- Produces:
  - `TaskStatus.awaiting_input`
  - `Task.awaiting_step: str | None`
  - Predicates: `is_active`=False, `is_finished`=False, `can_pause`=False, `can_resume`=True, `can_archive`=True, new `needs_input`=True.

- [ ] **Step 1: Write failing predicate test**

Add to `tests/test_task_status_predicates.py`:
```python
from vts.db.models import TaskStatus
from vts.services import task_status as ts


def test_awaiting_input_predicates():
    s = TaskStatus.awaiting_input
    assert ts.is_active(s) is False
    assert ts.is_finished(s) is False
    assert ts.can_pause(s) is False
    assert ts.can_resume(s) is True
    assert ts.can_archive(s) is True
    assert ts.needs_input(s) is True
    # No other status needs input.
    assert ts.needs_input(TaskStatus.running) is False
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_task_status_predicates.py -k awaiting_input -v`
Expected: FAIL — `AttributeError: awaiting_input` / `needs_input`.

- [ ] **Step 3: Add the status, field, and predicates**

In `vts/db/models.py`, add to `TaskStatus`:
```python
    awaiting_input = "awaiting_input"
```
Add to the `Task` model:
```python
    awaiting_step: Mapped[str | None] = mapped_column(String, nullable=True)
```
In `vts/services/task_status.py`:
```python
NEEDS_INPUT_STATUSES = {TaskStatus.awaiting_input}
# awaiting_input resumes only via the manual dialog, but can_resume stays True:
# the user can open it and bind nothing, so blocking resume would only add clicks.
RESUMABLE_STATUSES = {TaskStatus.paused, TaskStatus.failed, TaskStatus.awaiting_input}
ARCHIVABLE_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.awaiting_input}


def needs_input(status: TaskStatus) -> bool:
    return status in NEEDS_INPUT_STATUSES
```
Add `needs_input` into the `status_flags()` dict so the frontend receives it.

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_task_status_predicates.py -v`
Expected: PASS.

- [ ] **Step 5: Write and apply the migration**

Create `alembic/versions/0018_task_status_awaiting_input.py` (down_revision `0017_match_decisions` — see the migration-chain note in Task 9) mirroring `0013_task_status_waiting.py` (add the enum value AND the `awaiting_step` column). Copy the enum-alter pattern from `0003`/`0013`; add:
```python
def upgrade() -> None:
    # enum alter (non-native StrEnum stored as varchar check) — follow 0013's shape
    op.add_column("tasks", sa.Column("awaiting_step", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "awaiting_step")
```
(If `task_status` is a native enum in your migrations, add the value the same way `0013_task_status_waiting` does; if it is `native_enum=False` varchar, only the column add is needed. Match the existing head migration's approach exactly.)

Run: `alembic upgrade head`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add vts/db/models.py vts/services/task_status.py alembic/versions/0018_task_status_awaiting_input.py tests/test_task_status_predicates.py
git commit -m "feat(tasks): awaiting_input status + awaiting_step (vts-80i)"
```

---

## Task 9: MatchDecision model + repo + calibration query

**Files:**
- Modify: `vts/db/models.py`
- Modify: `vts/db/repo.py`
- Create: `alembic/versions/0017_match_decisions.py`
- Test: `tests/test_match_decision.py`

**Interfaces:**
- Produces:
  - `MatchDecision(id, user_id, source_task_id|None, speaker_label, speaker_id|None, voice_sample_id|None, distance|None, embedding_model, outcome, created_at)`
  - `record_decision(user_id, source_task_id, speaker_label, speaker_id, voice_sample_id, distance, embedding_model, outcome) -> MatchDecision`

- [ ] **Step 1: Write failing test (two-row override + model filter)**

Create `tests/test_match_decision.py`:
```python
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User, MatchDecision
from vts.db.repo import Repo
from _db import make_test_engine, ensure_pgvector

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_override_writes_two_rows(factory):
    async with factory() as s:
        repo = Repo(s)
        vasya = await repo.create_speaker(_USER, "Вася")
        petya = await repo.create_speaker(_USER, "Петя")
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S2",
            speaker_id=vasya.id, voice_sample_id=None, distance=0.67,
            embedding_model="m1", outcome="rejected")
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S2",
            speaker_id=petya.id, voice_sample_id=None, distance=0.65,
            embedding_model="m1", outcome="confirmed")
        await s.commit()
        rows = (await s.scalars(select(MatchDecision).order_by(MatchDecision.distance))).all()
        assert [(r.outcome, r.distance) for r in rows] == [("confirmed", 0.65), ("rejected", 0.67)]
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_match_decision.py -v`
Expected: FAIL — `ImportError: MatchDecision`.

- [ ] **Step 3: Add model, repo method, migration**

In `vts/db/models.py`:
```python
class MatchDecision(Base):
    __tablename__ = "match_decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    speaker_label: Mapped[str] = mapped_column(String)
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True)
    voice_sample_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_samples.id", ondelete="SET NULL"), nullable=True)
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```
In `vts/db/repo.py`:
```python
    async def record_decision(
        self, *, user_id: uuid.UUID, source_task_id: uuid.UUID | None, speaker_label: str,
        speaker_id: uuid.UUID | None, voice_sample_id: uuid.UUID | None,
        distance: float | None, embedding_model: str, outcome: str,
    ) -> MatchDecision:
        row = MatchDecision(
            user_id=user_id, source_task_id=source_task_id, speaker_label=speaker_label,
            speaker_id=speaker_id, voice_sample_id=voice_sample_id, distance=distance,
            embedding_model=embedding_model, outcome=outcome,
        )
        self.session.add(row)
        await self.session.flush()
        return row
```
Create `alembic/versions/0017_match_decisions.py` (down_revision `0016_voice_samples`), `create_table` mirroring the model columns with the same FK ondelete rules.

**Migration chain (linear, independent of task order):** `0013` → `0014_pgvector_extension` → `0015_speakers` → `0016_voice_samples` → `0017_match_decisions` → `0018_task_status_awaiting_input`. Task 8 produces `0018` but Task 9 produces `0017`; whichever task runs first, set `down_revision` to keep this exact chain — `0018`'s `down_revision` is `0017_match_decisions`, NOT `0016`. If Task 8 is implemented before Task 9, temporarily point `0018` at `0016` and fix it to `0017` when Task 9 lands, or implement Task 9 before Task 8.

- [ ] **Step 4: Run, verify passes**

Run: `pytest tests/test_match_decision.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/db/models.py vts/db/repo.py alembic/versions/0017_match_decisions.py tests/test_match_decision.py
git commit -m "feat(db): MatchDecision — record every match outcome for calibration (vts-80i)"
```

---

## Task 10: MatchSpeakersStep — the pipeline gate

**Files:**
- Create: `vts/pipeline/steps/speaker_match.py`
- Modify: `vts/pipeline/steps/registry.py`, `vts/pipeline/types.py` (DAG order: after `diarize`, before `prepare_summary_chunks`)
- Test: `tests/test_speaker_match_step.py`

**Interfaces:**
- Consumes: `diarization.json` (cluster embeddings + `embedding_model`), `repo.nearest_speakers`, `bucket`, `ctx.settings.speaker_match_*`, `diarize_enabled`.
- Produces: `outputs/speaker_matches.json` — `{speaker_label: {"outcome": str, "speaker_id": str|None, "distance": float|None, "candidates": [{"speaker_id","name","distance"}]}}`. Sets task → `awaiting_input` (via a raised `TaskPaused`-like signal or a status write + return) when any speaker is grey/miss and the no-stop flag is off.

- [ ] **Step 1: Write failing test — all-auto proceeds, any-grey pauses**

Create `tests/test_speaker_match_step.py`. Test the classification/decision function purely (factor it out of `run`):
```python
from vts.pipeline.steps.speaker_match import decide_pause


def test_all_auto_no_pause():
    matches = {"S0": {"outcome": "auto"}, "S1": {"outcome": "auto"}}
    assert decide_pause(matches, no_stop=False) is False


def test_grey_pauses_when_stop_allowed():
    matches = {"S0": {"outcome": "auto"}, "S1": {"outcome": "grey"}}
    assert decide_pause(matches, no_stop=False) is True


def test_no_stop_flag_never_pauses():
    matches = {"S0": {"outcome": "miss"}, "S1": {"outcome": "grey"}}
    assert decide_pause(matches, no_stop=True) is False
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_match_step.py -v`
Expected: FAIL — no module.

- [ ] **Step 3: Implement the step**

Create `vts/pipeline/steps/speaker_match.py`:
```python
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vts.pipeline.steps.base import Step, StepState
from vts.pipeline.steps.diarization import diarize_enabled
from vts.services.speaker_registry import bucket, MatchOutcome
from vts.services.storage import write_json

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


def decide_pause(matches: dict, no_stop: bool) -> bool:
    """Pause iff a human is needed: any speaker not auto-resolved and stops allowed."""
    if no_stop:
        return False
    return any(m["outcome"] != MatchOutcome.auto for m in matches.values())


class MatchSpeakersStep(Step):
    name = "match_speakers"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return (st.dirs["outputs"] / "speaker_matches.json").exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        default = bool(getattr(ctx.settings, "diarization_enabled_default", False))
        if not diarize_enabled(st.task_options, default):
            return True
        diar_path = st.dirs["outputs"] / "diarization.json"
        if not diar_path.exists():
            return True  # nothing to match
        diar = json.loads(diar_path.read_text(encoding="utf-8"))
        model = diar.get("embedding_model", "")
        embeddings = diar.get("embeddings", {})

        auto = ctx.settings.speaker_match_max_distance_auto
        cand = ctx.settings.speaker_match_max_distance_candidate
        matches: dict[str, dict] = {}
        async with ctx.session_factory() as session:
            from vts.db.repo import Repo
            repo = Repo(session)
            import uuid as _uuid
            for label, vector in embeddings.items():
                ranked = await repo.nearest_speakers(_uuid.UUID(st.user_id), vector, model)
                nearest = ranked[0] if ranked else None
                dist = nearest[1] if nearest else None
                outcome = bucket(dist, auto=auto, candidate=cand)
                matches[label] = {
                    "outcome": str(outcome),
                    "speaker_id": str(nearest[0].id) if (nearest and outcome == MatchOutcome.auto) else None,
                    "distance": dist,
                    "candidates": [
                        {"speaker_id": str(sp.id), "name": sp.name, "distance": d}
                        for sp, d in ranked
                    ],
                }
        write_json(st.dirs["outputs"] / "speaker_matches.json", matches)

        no_stop = ctx.task_flag(st.task_options, "speaker_no_manual_stop", default=False)
        if decide_pause(matches, no_stop):
            await ctx.set_awaiting_input(st.task_id, "match_speakers")
            raise ctx.TaskAwaitingInput()  # or the existing pause-signal; see Step 4
        # auto-only (or no_stop): persist the auto bindings and continue
        return True
```
(Implementer: the pause mechanism must mirror the existing `TaskPaused` flow in `processor.py`. Add `set_awaiting_input(task_id, step)` to the context/repo and a signal the processor catches to set status `awaiting_input` and release the worker — reuse the `TaskPaused` machinery rather than adding a parallel path. `ctx.task_flag` already exists.)

- [ ] **Step 4: Wire the pause into the processor**

In `vts/pipeline/processor.py`, handle the awaiting-input signal like `TaskPaused`: set task status to `TaskStatus.awaiting_input`, persist `awaiting_step`, stop processing, release the lane/slot. Add `Repo.set_awaiting_input(task_id, step)` that writes both fields.

- [ ] **Step 5: Register the step in the DAG**

In `vts/pipeline/steps/registry.py` add `MatchSpeakersStep` to `resolve_step`. In `vts/pipeline/types.py` (`build_dag_steps`), insert `match_speakers` after `diarize` and before `prepare_summary_chunks`.

- [ ] **Step 6: Run, verify passes**

Run: `pytest tests/test_speaker_match_step.py -v`
Expected: PASS (the `decide_pause` unit tests; full-step behavior is covered live in Task 12).

- [ ] **Step 7: Commit**

```bash
git add vts/pipeline/steps/speaker_match.py vts/pipeline/steps/registry.py vts/pipeline/types.py vts/pipeline/processor.py vts/db/repo.py tests/test_speaker_match_step.py
git commit -m "feat(pipeline): match_speakers step gates on registry, pauses for review (vts-80i)"
```

---

## Task 11: API — registry CRUD, sample audio, voice-resolution

**Files:**
- Modify: `vts/api/main.py`
- Test: `tests/test_speaker_api.py`

**Interfaces:**
- Consumes: repo methods from Tasks 3, 9; `load_sample_audio`.
- Produces endpoints:
  - `GET /api/speakers` → `[{id, name, sample_count}]`
  - `POST /api/speakers` `{name}` → speaker
  - `PATCH /api/speakers/{id}` `{name}` → speaker (rename)
  - `DELETE /api/speakers/{id}` → 204
  - `GET /api/speakers/{id}/samples` → `[{id, duration_sec, source_task_id, created_at}]`
  - `DELETE /api/speakers/{id}/samples/{sample_id}` → 204
  - `GET /api/speakers/samples/{sample_id}/audio` → wav bytes (streams undeferred audio)
  - `POST /api/tasks/{task_id}/speakers` — resolution payload → applies bindings + fragments + decisions in ONE transaction, optionally resumes.

- [ ] **Step 1: Write failing API tests**

Create `tests/test_speaker_api.py` (uses the `client` fixture):
```python
import pytest


@pytest.mark.asyncio
async def test_speaker_crud_via_api(client):
    r = await client.post("/api/speakers", json={"name": "Вася"})
    assert r.status_code == 200
    sid = r.json()["id"]
    r = await client.get("/api/speakers")
    assert any(s["name"] == "Вася" for s in r.json())
    r = await client.patch(f"/api/speakers/{sid}", json={"name": "Василий"})
    assert r.json()["name"] == "Василий"
    r = await client.delete(f"/api/speakers/{sid}")
    assert r.status_code == 204
    r = await client.get("/api/speakers")
    assert all(s["name"] != "Василий" for s in r.json())


@pytest.mark.asyncio
async def test_delete_missing_speaker_404(client):
    import uuid
    r = await client.delete(f"/api/speakers/{uuid.uuid4()}")
    assert r.status_code == 404
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_api.py -v`
Expected: FAIL — 404 routing / no endpoint.

- [ ] **Step 3: Implement CRUD + audio endpoints**

In `vts/api/main.py`, add Pydantic models (`SpeakerOut`, `SpeakerCreateRequest`, `SpeakerUpdateRequest`, `VoiceSampleOut`) and endpoints mirroring the `/api/prompts` block (same auth dep `get_current_user`, `Repo(session)`, `await session.commit()`). `GET /api/speakers` returns `sample_count` via `len(await repo.list_voice_samples(...))` or a count query. The audio endpoint:
```python
    @app.get("/api/speakers/samples/{sample_id}/audio")
    async def get_sample_audio(
        sample_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        loaded = await repo.load_sample_audio(uuid.UUID(user.id), sample_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        audio, fmt = loaded
        return Response(content=audio, media_type=f"audio/{fmt}")
```

- [ ] **Step 4: Run CRUD tests, verify pass**

Run: `pytest tests/test_speaker_api.py -v`
Expected: PASS.

- [ ] **Step 5: Write failing test for the resolution endpoint (transactional)**

Add to `tests/test_speaker_api.py` a test that `POST /api/tasks/{id}/speakers` with a binding to a new speaker creates the speaker, a voice sample, and a match decision — and that a payload failing mid-way writes nothing. (Seed a task row via the factory; assert all-or-nothing by sending an invalid second binding and checking the first was rolled back.)

- [ ] **Step 6: Implement the resolution endpoint**

Add `POST /api/tasks/{task_id}/speakers`. Payload shape:
```python
class VoiceResolution(BaseModel):
    speaker_label: str
    action: str  # "bind_existing" | "bind_new" | "leave_anonymous" | "accept_auto"
    speaker_id: str | None = None
    new_name: str | None = None
    add_fragment: bool = True
    distance: float | None = None
    voice_sample_id: str | None = None  # winning fragment for the decision
    outcome: str  # confirmed | rejected | manual_match | auto_accepted | auto_overridden | left_anonymous

class VoiceResolutionRequest(BaseModel):
    resolutions: list[VoiceResolution]
    continue_task: bool
```
In the handler, open ONE transaction: for each resolution create/get the speaker, optionally add a fragment (cut audio already lives in `outputs/preview_*.wav`; read the bytes, call `repo.add_voice_sample` with the embedding from the sidecar `/embed` on that clip, `source_task_id=task_id`), and `repo.record_decision(...)`. On rollback (any error), nothing persists. If `continue_task`, set task back to `queued` (resume from the next step); else leave `awaiting_input`. Commit once at the end.

- [ ] **Step 7: Run resolution tests, verify pass**

Run: `pytest tests/test_speaker_api.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add vts/api/main.py tests/test_speaker_api.py
git commit -m "feat(api): speaker registry CRUD, sample audio, voice resolution (vts-80i)"
```

---

## Task 13: Registry dialog UI

**Files:**
- Modify: `vts/static/index.html` (DOM before `<script>`), `vts/static/app.js`, `vts/static/styles.css`
- Verify: `verifier-web` skill

**Interfaces:**
- Consumes: `/api/speakers`, `/api/speakers/{id}` (rename/delete), `/api/speakers/{id}/samples`, sample audio + delete endpoints.

- [ ] **Step 1: Add the two-column dialog markup**

In `index.html`, before the `<script>` tag (app.js has no `defer`), add a `<dialog id="speaker-registry-dialog">` with a left `<ul id="speaker-list">` and right `<ul id="speaker-samples">`. Include an audio element placeholder for previews.

- [ ] **Step 2: Wire list/rename/delete/samples in app.js**

Add functions: `openSpeakerRegistry()`, `renderSpeakers(list)`, inline rename (mirror the task-rename pattern already in app.js), `deleteSpeaker(id)` with a confirm that names the sample count, `renderSamples(speakerId)`, `deleteSample(id)` with confirm, and per-sample `<audio src="/api/speakers/samples/{id}/audio">`. All deletions confirm (forbidden-actions pattern).

- [ ] **Step 3: Verify in a real browser**

Run the `verifier-web` skill: open the registry dialog with stubbed `/api/speakers*`, assert two columns render, rename is inline, deletions prompt.

- [ ] **Step 4: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css
git commit -m "feat(ui): voice registry dialog — speakers and their fragments (vts-80i)"
```

---

## Task 14: Voice-resolution dialog + "Доработать" button + create-form checkbox

**Files:**
- Modify: `vts/static/index.html`, `vts/static/app.js`, `vts/static/styles.css`
- Verify: `verifier-web`

**Interfaces:**
- Consumes: `speaker_matches.json` served via a task endpoint (add `GET /api/tasks/{id}/speaker-matches` returning the JSON), `POST /api/tasks/{id}/speakers`, task preview audio, `needs_input` status flag.

- [ ] **Step 1: Add "Доработать" button for needs_input tasks**

In app.js task rendering, when `statusFlags[status].needs_input`, show a "Доработать" button that dispatches on `task.awaiting_step` (only `match_speakers` today) to `openVoiceDialog(taskId)`.

- [ ] **Step 2: Add the resolution dialog markup (before `<script>`)**

`<dialog id="voice-resolution-dialog">` with a `<ul id="voice-list">` and three buttons: `#voice-save`, `#voice-save-continue`, `#voice-cancel`.

- [ ] **Step 3: Render voices with status glyphs + all-candidates dropdown**

For each voice: preview `<audio>`, a glyph (🟢/🟡/🔴 from `outcome`), and one `<select>` listing ALL user speakers sorted by distance plus `<Добавить новую персону>`. Preselect: nearest for grey/auto, "new" for miss. Grey/auto with an "add fragment" checkbox (default on).

- [ ] **Step 4: Wire the three buttons with confirmations**

- `#voice-save`: POST resolutions, `continue_task=false`, stay in dialog-closed `awaiting_input`.
- `#voice-save-continue`: if any voice left anonymous, confirm ("останутся анонимными как Голос 1, Голос 2…"); POST with `continue_task=true`.
- `#voice-cancel`: if dirty, confirm discard; else close. Same for Esc/backdrop.
- Rebind with fragment rollback: when overriding a previously-saved binding, confirm ("фрагмент, добавленный к X, будет удалён").
- Edit after summarization started: confirm ("учтутся только при перезапуске суммаризации").

- [ ] **Step 5: Add the create-form checkbox**

In the task-create form, add "не останавливаться для ручной доработки" (`speaker_no_manual_stop`), enabled only when `diarize` is checked; include it in preset options.

- [ ] **Step 6: Verify in a real browser**

Run `verifier-web`: stub `/api/tasks/{id}/speaker-matches` with one of each glyph; assert dropdown lists all candidates sorted, three buttons behave, confirmations fire, dirty-cancel prompts.

- [ ] **Step 7: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css vts/api/main.py
git commit -m "feat(ui): voice-resolution dialog, Доработать button, no-stop flag (vts-80i)"
```

---

## Task 15: End-to-end + version bump + docs

**Files:**
- Modify: `vts/__init__.py` (version bump)
- Modify: `docs/` deploy/config notes (new settings, sidecar version)
- Test: full suite

**Interfaces:** none new.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 2: Document the new config + sidecar version**

Add the five `speaker_*` settings and the `speaker_no_manual_stop` task flag to the config docs; note the sidecar minor bump and that `/embed` is required by matching.

- [ ] **Step 3: Bump version**

Bump `vts/__init__.py` to the next version.

- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py docs/
git commit -m "chore(release): speaker registry — config docs, version bump (vts-80i)"
```

---

## Self-Review Notes

- **Spec coverage:** models (T2/T9), pgvector parity (T1), CRUD (T3/T11), matching + thresholds (T4), sidecar /embed + model id (T5/T6), cluster/fragment shared-space validation (T6.5), preview cutting (T7), awaiting_input + predicates (T8), match_speakers gate + pause (T10), MatchDecision + two-row override (T9), registry dialog (T13), resolution dialog + all-candidates + three buttons + confirmations + no-stop flag (T14). Merge (vts-552) and model-change migration (vts-ojb) are explicitly out of scope.
- **Ordering:** T2 hardcodes 256, already confirmed live in brainstorm. T6.5 (moved up from the old T12 per user decision 2026-07-17) is the gate for the shared-space assumption — it runs right after the client `embed()` lands and BEFORE Tasks 7/10 build on it, so a broken premise surfaces on day one rather than at the end.
- **Deferred unknowns flagged inline:** exact pyannote 4.x standalone-embedding load call (T5), exact ffmpeg wav-cut flags (T7), the processor pause-signal reuse (T10) — each notes "validate live / mirror existing pattern" rather than inventing an interface.
