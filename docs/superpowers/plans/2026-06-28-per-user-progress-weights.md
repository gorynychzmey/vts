# Per-user Adaptive Progress Weights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make progress-bar step weights per-user, stored in Postgres, recomputed periodically by the worker from each user's completed runs, served to the client via an endpoint, with the hardcoded `app.js` constants kept only as an offline fallback.

**Architecture:** Pure math extends `vts/metrics/step_weights.py` (window_offset, sample counts, seed merge, fallback). A new `user_step_weights` table (migration 0012) holds computed results. `Repo` gains collection + upsert + read methods. A service orchestrates recompute; the worker runs it on a periodic loop. A `GET /api/progress-weights` endpoint serves the effective user's weights (impersonation-correct by reusing `user.id`). `app.js` fetches them in `bootstrap()` and falls back to its constants on failure.

**Tech Stack:** Python 3, SQLAlchemy async, Alembic, FastAPI, Pydantic, pytest (+ Postgres fixture). Client: vanilla JS (`vts/static/app.js`).

## Global Constraints

- Self-hosted / on-prem: no external services, no network calls at runtime (project CLAUDE.md).
- Median, not mean. Only `status=completed` steps of `status=completed` tasks count.
- `window_offset` default is `0` so vts-b6t's existing script and its 6 tests stay green; per-user path uses `window_offset=1` (divide by true window count `total-1`).
- `min_samples` default 5; per-step threshold — a step with fewer samples keeps its seed value.
- Recompute interval default 604800s (1 week); `progress_weights_enabled` default True.
- Impersonation: the endpoint scopes by `uuid.UUID(user.id)` (already the effective acting_as user's id) exactly like `/api/prompts`; never use `requested_by`. The worker recompute has no request context and attributes durations by `tasks.user_id`.
- Migration revision id format is the full slug, e.g. `0012_user_step_weights`, `down_revision = "0011_presets"`.
- Config fields live on `Settings` in `vts/core/config.py` with `env_prefix="VTS_"` (so `VTS_PROGRESS_WEIGHTS_*`).
- Seed values (per real window for summarize_windows): download 5.5, extract_audio 2.0, trim_initial_silence 0.3, segment_audio 1.2, detect_language 2.6, transcribe_segments 174.8, merge_transcript 0.1, prepare_llama_model 6.3, prepare_summary_chunks 0.1, summarize_windows 74.8; final summary fallback 514.4.
- Bump `vts/__init__.py __version__` once, in the client-facing task (Task 6).
- Python interpreter: `/home/victor/dev/vts/.venv/bin/python`. Postgres tests need `VTS_TEST_DATABASE_URL` (see existing `tests/` fixtures, e.g. `tests/test_presets_api.py`).

---

### Task 1: Extend pure metrics — window_offset, sample counts, seed merge, fallback, seed constants

**Files:**
- Modify: `vts/metrics/step_weights.py`
- Modify: `tests/test_step_weights.py` (add cases; keep existing 6 green)

**Interfaces:**
- Consumes: existing `StepDuration`, `median`, `aggregate_step_weights` (b6t).
- Produces (relied on by Tasks 3, 5):
  - `aggregate_step_weights(rows, *, window_offset: int = 0) -> dict[str, float]` — for `summarize_windows`, divisor is `window_total - window_offset`; rows where that is `< 1` (or window_total not int) are skipped for that step.
  - `step_sample_counts(rows, *, window_offset: int = 0) -> dict[str, int]` — `{step_name: n}` valid samples per step (same window guard as aggregate).
  - `merge_with_seed(computed: dict[str, float], sample_counts: dict[str, int], *, min_samples: int, seed: dict[str, float]) -> dict[str, float]` — per step in `seed`: use `computed[step]` if `sample_counts.get(step, 0) >= min_samples`, else `seed[step]`. Returns all seed steps.
  - `final_summary_fallback(rows, *, min_samples: int, seed_fallback: float) -> float` — median of durations of rows with `name == "summarize_final"` if count `>= min_samples`, else `seed_fallback`.
  - `SEED_STEP_WEIGHTS: dict[str, float]` and `SEED_FINAL_SUMMARY_FALLBACK: float` — the Global-Constraints seed values.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_step_weights.py`)

```python
from vts.metrics.step_weights import (
    StepDuration,
    aggregate_step_weights,
    step_sample_counts,
    merge_with_seed,
    final_summary_fallback,
    SEED_STEP_WEIGHTS,
    SEED_FINAL_SUMMARY_FALLBACK,
)


def test_window_offset_divides_by_true_window_count():
    # 100s over total=11 -> with offset=1 divide by 10 -> 10.0/window
    rows = [StepDuration("summarize_windows", 100.0, 11)]
    assert aggregate_step_weights(rows, window_offset=1) == {"summarize_windows": 10.0}
    # default offset=0 keeps b6t behavior: divide by 11 -> 9.1
    assert aggregate_step_weights(rows) == {"summarize_windows": 9.1}


def test_window_offset_skips_when_true_count_below_one():
    rows = [StepDuration("summarize_windows", 50.0, 1)]  # offset=1 -> 0 -> skip
    assert aggregate_step_weights(rows, window_offset=1) == {}


def test_step_sample_counts_counts_valid_rows():
    rows = [
        StepDuration("download", 5.0, None),
        StepDuration("download", 6.0, None),
        StepDuration("summarize_windows", 100.0, 11),  # valid at offset=1
        StepDuration("summarize_windows", 50.0, 1),     # invalid at offset=1
    ]
    counts = step_sample_counts(rows, window_offset=1)
    assert counts["download"] == 2
    assert counts["summarize_windows"] == 1


def test_merge_with_seed_below_threshold_keeps_seed():
    seed = {"download": 5.5, "extract_audio": 2.0}
    computed = {"download": 99.0, "extract_audio": 88.0}
    counts = {"download": 10, "extract_audio": 2}
    merged = merge_with_seed(computed, counts, min_samples=5, seed=seed)
    assert merged == {"download": 99.0, "extract_audio": 2.0}


def test_merge_with_seed_missing_computed_uses_seed():
    seed = {"download": 5.5, "merge_transcript": 0.1}
    merged = merge_with_seed({}, {}, min_samples=5, seed=seed)
    assert merged == seed


def test_final_summary_fallback_threshold():
    rows = [StepDuration("summarize_final", v, None) for v in (400.0, 500.0, 600.0)]
    # 3 < min_samples 5 -> seed
    assert final_summary_fallback(rows, min_samples=5, seed_fallback=514.4) == 514.4
    # >= threshold -> median
    assert final_summary_fallback(rows, min_samples=3, seed_fallback=514.4) == 500.0


def test_seed_constants_present():
    assert SEED_STEP_WEIGHTS["summarize_windows"] == 74.8
    assert SEED_STEP_WEIGHTS["transcribe_segments"] == 174.8
    assert SEED_FINAL_SUMMARY_FALLBACK == 514.4
    assert len(SEED_STEP_WEIGHTS) == 10
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights.py -v`
Expected: FAIL — `ImportError: cannot import name 'step_sample_counts'` (and the other new names).

- [ ] **Step 3: Implement** (replace the body of `vts/metrics/step_weights.py` from the `aggregate_step_weights` definition onward, keeping `StepDuration`, `median`, `_PER_WINDOW_STEP`)

```python
def _per_window_divisor(window_total, window_offset: int) -> int | None:
    if not isinstance(window_total, int):
        return None
    divisor = window_total - window_offset
    return divisor if divisor >= 1 else None


def aggregate_step_weights(rows: list[StepDuration], *, window_offset: int = 0) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            divisor = _per_window_divisor(row.window_total, window_offset)
            if divisor is None:
                continue
            buckets.setdefault(row.name, []).append(row.duration_sec / divisor)
        else:
            buckets.setdefault(row.name, []).append(row.duration_sec)
    return {name: round(median(vals), 1) for name, vals in buckets.items() if vals}


def step_sample_counts(rows: list[StepDuration], *, window_offset: int = 0) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            if _per_window_divisor(row.window_total, window_offset) is None:
                continue
        counts[row.name] = counts.get(row.name, 0) + 1
    return counts


def merge_with_seed(
    computed: dict[str, float],
    sample_counts: dict[str, int],
    *,
    min_samples: int,
    seed: dict[str, float],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for step, seed_value in seed.items():
        if sample_counts.get(step, 0) >= min_samples and step in computed:
            out[step] = computed[step]
        else:
            out[step] = seed_value
    return out


def final_summary_fallback(rows: list[StepDuration], *, min_samples: int, seed_fallback: float) -> float:
    finals = [r.duration_sec for r in rows if r.name == "summarize_final"]
    if len(finals) >= min_samples:
        return round(median(finals), 1)
    return seed_fallback


# Seed weights recomputed one-off in vts-b6t (medians over completed runs,
# 2026-06-28). summarize_windows is per REAL window (total-1 convention).
SEED_STEP_WEIGHTS: dict[str, float] = {
    "download": 5.5,
    "extract_audio": 2.0,
    "trim_initial_silence": 0.3,
    "segment_audio": 1.2,
    "detect_language": 2.6,
    "transcribe_segments": 174.8,
    "merge_transcript": 0.1,
    "prepare_llama_model": 6.3,
    "prepare_summary_chunks": 0.1,
    "summarize_windows": 74.8,
}
SEED_FINAL_SUMMARY_FALLBACK: float = 514.4
```

Note: also update the module docstring's mention to reflect the offset param if convenient; not required.

- [ ] **Step 4: Run to verify pass** (new + existing b6t tests)

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights.py -v`
Expected: PASS — all 6 original + 7 new = 13 passed.

- [ ] **Step 5: Commit**

```bash
git add vts/metrics/step_weights.py tests/test_step_weights.py
git commit -m "feat(metrics): window_offset, sample counts, seed merge, fallback for per-user weights (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Model + migration for `user_step_weights`

**Files:**
- Modify: `vts/db/models.py` (add `UserStepWeights`)
- Create: `alembic/versions/0012_user_step_weights.py`
- Test: `tests/test_user_step_weights_migration.py`

**Interfaces:**
- Produces (relied on by Task 3): `UserStepWeights` ORM model with columns `id`, `user_id`, `weights` (JSON), `final_summary_fallback` (Float, nullable), `computed_at` (DateTime tz), `sample_counts` (JSON); unique on `user_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_step_weights_migration.py
import uuid
import pytest
from vts.db.models import UserStepWeights, User


def test_model_columns_exist():
    cols = set(UserStepWeights.__table__.columns.keys())
    assert cols == {"id", "user_id", "weights", "final_summary_fallback", "computed_at", "sample_counts"}


def test_user_id_unique_constraint():
    uniques = [c for c in UserStepWeights.__table__.constraints
               if c.__class__.__name__ == "UniqueConstraint"]
    cols = {tuple(c.columns.keys()) for c in uniques}
    assert ("user_id",) in cols
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_user_step_weights_migration.py -v`
Expected: FAIL — `ImportError: cannot import name 'UserStepWeights'`.

- [ ] **Step 3: Add the model** (append to `vts/db/models.py`, after `AsrSegment`)

```python
class UserStepWeights(Base):
    __tablename__ = "user_step_weights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    final_summary_fallback: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sample_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_step_weights_user"),
        Index("ix_user_step_weights_user", "user_id"),
    )
```

- [ ] **Step 4: Write the migration** (`alembic/versions/0012_user_step_weights.py`)

```python
"""Add user_step_weights table (vts-8cm)."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_user_step_weights"
down_revision = "0011_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_step_weights",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("final_summary_fallback", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_counts", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_step_weights_user"),
    )
    op.create_index("ix_user_step_weights_user", "user_step_weights", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_step_weights_user", table_name="user_step_weights")
    op.drop_table("user_step_weights")
```

- [ ] **Step 5: Run model tests + verify migration chains**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_user_step_weights_migration.py -v`
Expected: PASS (2 passed).
Run: `/home/victor/dev/vts/.venv/bin/python -m alembic history | head -3`
Expected: shows `0012_user_step_weights` with down_revision `0011_presets` (no branch error). If `alembic` needs a DB URL and none is set, instead run `/home/victor/dev/vts/.venv/bin/python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print([r.revision for r in s.walk_revisions()][:3])"` and confirm `0012_user_step_weights` is the head chaining to `0011_presets`.

- [ ] **Step 6: Commit**

```bash
git add vts/db/models.py alembic/versions/0012_user_step_weights.py tests/test_user_step_weights_migration.py
git commit -m "feat(db): user_step_weights model + migration 0012 (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Repo methods — collect durations, upsert, read, list users

**Files:**
- Modify: `vts/db/repo.py`
- Test: `tests/test_user_step_weights_repo.py` (Postgres fixture)

**Interfaces:**
- Consumes: `StepDuration` from `vts.metrics.step_weights`; `UserStepWeights`, `Step`, `Task`, `TaskStatus`, `StepStatus`, `User` models.
- Produces (relied on by Tasks 4, 5):
  - `Repo.step_durations_for_user(user_id: uuid.UUID) -> list[StepDuration]`
  - `Repo.upsert_user_step_weights(user_id, weights: dict, final_summary_fallback: float | None, computed_at: datetime, sample_counts: dict) -> UserStepWeights`
  - `Repo.get_user_step_weights(user_id) -> UserStepWeights | None`
  - `Repo.users_with_completed_tasks() -> list[uuid.UUID]`

- [ ] **Step 1: Write the failing test** (mirror the Postgres-fixture style of `tests/test_presets_api.py` / existing repo tests — use the same session fixture they use)

```python
# tests/test_user_step_weights_repo.py
import uuid
from datetime import datetime, timezone
import pytest
from vts.db.repo import Repo
from vts.db.models import Task, Step, TaskStatus, StepStatus
from vts.metrics.step_weights import StepDuration

pytestmark = pytest.mark.asyncio


async def _make_completed_task(repo, user_id, total_windows, step_specs):
    # step_specs: list[(name, started, finished, status)]
    task = Task(
        user_id=user_id, source_url="u", status=TaskStatus.completed,
        options={}, artifact_dir="/tmp/x", summary_progress={"current": total_windows, "total": total_windows},
    )
    repo.session.add(task)
    await repo.session.flush()
    for name, started, finished, status in step_specs:
        repo.session.add(Step(task_id=task.id, name=name, status=status,
                              started_at=started, finished_at=finished))
    await repo.session.flush()
    return task


async def test_step_durations_for_user_only_completed(db_session):
    repo = Repo(db_session)
    user = await repo.get_or_create_user("durations@example.com")
    await db_session.flush()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta
    await _make_completed_task(repo, user.id, 6, [
        ("download", t0, t0 + timedelta(seconds=10), StepStatus.completed),
        ("summarize_windows", t0, t0 + timedelta(seconds=60), StepStatus.completed),
        ("merge_transcript", t0, t0 + timedelta(seconds=5), StepStatus.failed),  # excluded
    ])
    await db_session.commit()
    rows = await repo.step_durations_for_user(user.id)
    names = sorted(r.name for r in rows)
    assert names == ["download", "summarize_windows"]
    sw = next(r for r in rows if r.name == "summarize_windows")
    assert sw.window_total == 6
    assert abs(sw.duration_sec - 60.0) < 0.01


async def test_upsert_and_get_user_step_weights(db_session):
    repo = Repo(db_session)
    user = await repo.get_or_create_user("upsert@example.com")
    await db_session.flush()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    await repo.upsert_user_step_weights(user.id, {"download": 9.0}, 500.0, now, {"download": 7})
    await db_session.commit()
    row = await repo.get_user_step_weights(user.id)
    assert row.weights == {"download": 9.0}
    assert row.final_summary_fallback == 500.0
    # upsert again -> single row, updated
    await repo.upsert_user_step_weights(user.id, {"download": 1.0}, 1.0, now, {"download": 1})
    await db_session.commit()
    row2 = await repo.get_user_step_weights(user.id)
    assert row2.weights == {"download": 1.0}


async def test_users_with_completed_tasks(db_session):
    repo = Repo(db_session)
    u1 = await repo.get_or_create_user("has-completed@example.com")
    u2 = await repo.get_or_create_user("no-completed@example.com")
    await db_session.flush()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await _make_completed_task(repo, u1.id, 3, [])
    db_session.add(Task(user_id=u2.id, source_url="u", status=TaskStatus.queued,
                        options={}, artifact_dir="/tmp/y"))
    await db_session.commit()
    ids = await repo.users_with_completed_tasks()
    assert u1.id in ids
    assert u2.id not in ids
```

(If the repo-test session fixture has a different name than `db_session`, use whatever the existing repo/API tests use — check `tests/conftest.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_user_step_weights_repo.py -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'step_durations_for_user'`.

- [ ] **Step 3: Implement** (add to `vts/db/repo.py`; add `UserStepWeights` and `Float` usage via existing model import — update the model import line to include `UserStepWeights`)

Update the import line at the top:
```python
from vts.db.models import ApiToken, AsrSegment, Preset, Prompt, Step, StepStatus, Task, TaskStatus, User, UserStepWeights
```
Add `from vts.metrics.step_weights import StepDuration` near the top imports.

Add these methods to `Repo`:
```python
    # ------------------------------------------------------------------
    # Per-user step weights (vts-8cm)
    # ------------------------------------------------------------------

    async def step_durations_for_user(self, user_id: uuid.UUID) -> list[StepDuration]:
        stmt = (
            select(Step.name, Step.started_at, Step.finished_at, Task.summary_progress)
            .join(Task, Step.task_id == Task.id)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.completed,
                Step.status == StepStatus.completed,
                Step.started_at.is_not(None),
                Step.finished_at.is_not(None),
            )
        )
        rows: list[StepDuration] = []
        for name, started, finished, summary_progress in await self.session.execute(stmt):
            duration = (finished - started).total_seconds()
            if duration < 0:
                continue
            total = None
            if isinstance(summary_progress, dict):
                raw_total = summary_progress.get("total")
                if isinstance(raw_total, int) and raw_total >= 1:
                    total = raw_total
            rows.append(StepDuration(name, duration, total))
        return rows

    async def upsert_user_step_weights(
        self,
        user_id: uuid.UUID,
        weights: dict,
        final_summary_fallback: float | None,
        computed_at: datetime,
        sample_counts: dict,
    ) -> UserStepWeights:
        row = await self.get_user_step_weights(user_id)
        if row is None:
            row = UserStepWeights(user_id=user_id)
            self.session.add(row)
        row.weights = weights
        row.final_summary_fallback = final_summary_fallback
        row.computed_at = computed_at
        row.sample_counts = sample_counts
        await self.session.flush()
        return row

    async def get_user_step_weights(self, user_id: uuid.UUID) -> UserStepWeights | None:
        return await self.session.scalar(
            select(UserStepWeights).where(UserStepWeights.user_id == user_id)
        )

    async def users_with_completed_tasks(self) -> list[uuid.UUID]:
        stmt = (
            select(Task.user_id)
            .where(Task.status == TaskStatus.completed)
            .distinct()
        )
        return list(await self.session.scalars(stmt))
```

- [ ] **Step 4: Run to verify pass**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_user_step_weights_repo.py -v`
Expected: PASS (3 passed). If it errors on missing table, run migrations against the test DB the way the existing Postgres tests do (check conftest — they auto-create schema from `Base.metadata`).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_user_step_weights_repo.py
git commit -m "feat(db): repo methods for per-user step weight durations + upsert (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Recompute service

**Files:**
- Create: `vts/services/step_weights_recompute.py`
- Test: `tests/test_step_weights_recompute.py` (Postgres fixture)

**Interfaces:**
- Consumes: Task 1 metrics (`aggregate_step_weights`, `step_sample_counts`, `merge_with_seed`, `final_summary_fallback`, `SEED_STEP_WEIGHTS`, `SEED_FINAL_SUMMARY_FALLBACK`); Task 3 repo methods; `SessionLocal` / a session factory.
- Produces (relied on by Task 5):
  - `async recompute_for_user(session, user_id, *, min_samples, seed=SEED_STEP_WEIGHTS, seed_fallback=SEED_FINAL_SUMMARY_FALLBACK) -> bool`
  - `async recompute_all_users(session_factory, *, min_samples, seed=SEED_STEP_WEIGHTS, seed_fallback=SEED_FINAL_SUMMARY_FALLBACK) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_step_weights_recompute.py
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from vts.db.repo import Repo
from vts.db.models import Task, Step, TaskStatus, StepStatus
from vts.services.step_weights_recompute import recompute_for_user
from vts.metrics.step_weights import SEED_STEP_WEIGHTS

pytestmark = pytest.mark.asyncio


async def _completed_task(session, user_id, total, steps):
    t = Task(user_id=user_id, source_url="u", status=TaskStatus.completed,
             options={}, artifact_dir="/tmp/x",
             summary_progress={"current": total, "total": total})
    session.add(t)
    await session.flush()
    for name, dur in steps:
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.add(Step(task_id=t.id, name=name, status=StepStatus.completed,
                         started_at=t0, finished_at=t0 + timedelta(seconds=dur)))
    await session.flush()


async def test_recompute_below_threshold_keeps_seed(db_session):
    repo = Repo(db_session)
    user = await repo.get_or_create_user("recompute1@example.com")
    await db_session.flush()
    # Only 2 download samples (< default 5) -> seed kept for download
    await _completed_task(db_session, user.id, 6, [("download", 99.0)])
    await _completed_task(db_session, user.id, 6, [("download", 99.0)])
    await db_session.commit()
    wrote = await recompute_for_user(db_session, user.id, min_samples=5)
    await db_session.commit()
    assert wrote is True
    row = await repo.get_user_step_weights(user.id)
    assert row.weights["download"] == SEED_STEP_WEIGHTS["download"]  # seed, not 99.0


async def test_recompute_above_threshold_uses_computed(db_session):
    repo = Repo(db_session)
    user = await repo.get_or_create_user("recompute2@example.com")
    await db_session.flush()
    for _ in range(5):
        await _completed_task(db_session, user.id, 6, [("download", 42.0)])
    await db_session.commit()
    await recompute_for_user(db_session, user.id, min_samples=5)
    await db_session.commit()
    row = await repo.get_user_step_weights(user.id)
    assert row.weights["download"] == 42.0
    assert row.sample_counts["download"] == 5


async def test_recompute_no_data_returns_false(db_session):
    repo = Repo(db_session)
    user = await repo.get_or_create_user("recompute3@example.com")
    await db_session.flush()
    await db_session.commit()
    wrote = await recompute_for_user(db_session, user.id, min_samples=5)
    assert wrote is False
    assert await repo.get_user_step_weights(user.id) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights_recompute.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.services.step_weights_recompute'`.

- [ ] **Step 3: Implement** (`vts/services/step_weights_recompute.py`)

```python
"""Per-user progress-weight recompute (vts-8cm).

Orchestrates the pure metrics + repo persistence. Math lives in
vts.metrics.step_weights; SQL lives in vts.db.repo. This module only wires them.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.repo import Repo
from vts.metrics.step_weights import (
    SEED_FINAL_SUMMARY_FALLBACK,
    SEED_STEP_WEIGHTS,
    aggregate_step_weights,
    final_summary_fallback,
    merge_with_seed,
    step_sample_counts,
)

logger = logging.getLogger("vts.step_weights")

# Per-user durations are normalized per REAL window (total - 1), matching app.js.
_WINDOW_OFFSET = 1


async def recompute_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    min_samples: int,
    seed: dict[str, float] = SEED_STEP_WEIGHTS,
    seed_fallback: float = SEED_FINAL_SUMMARY_FALLBACK,
) -> bool:
    repo = Repo(session)
    rows = await repo.step_durations_for_user(user_id)
    if not rows:
        return False
    computed = aggregate_step_weights(rows, window_offset=_WINDOW_OFFSET)
    counts = step_sample_counts(rows, window_offset=_WINDOW_OFFSET)
    weights = merge_with_seed(computed, counts, min_samples=min_samples, seed=seed)
    fallback = final_summary_fallback(rows, min_samples=min_samples, seed_fallback=seed_fallback)
    await repo.upsert_user_step_weights(
        user_id, weights, fallback, datetime.now(tz=timezone.utc), counts
    )
    return True


async def recompute_all_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    min_samples: int,
    seed: dict[str, float] = SEED_STEP_WEIGHTS,
    seed_fallback: float = SEED_FINAL_SUMMARY_FALLBACK,
) -> int:
    async with session_factory() as session:
        repo = Repo(session)
        user_ids = await repo.users_with_completed_tasks()
    updated = 0
    for user_id in user_ids:
        try:
            async with session_factory() as session:
                wrote = await recompute_for_user(
                    session, user_id, min_samples=min_samples, seed=seed, seed_fallback=seed_fallback
                )
                await session.commit()
            if wrote:
                updated += 1
        except Exception:  # one user's failure must not abort the sweep
            logger.exception("step-weights recompute failed for user %s", user_id)
    logger.info("step-weights recompute done: %s/%s users updated", updated, len(user_ids))
    return updated
```

- [ ] **Step 4: Run to verify pass**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights_recompute.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add vts/services/step_weights_recompute.py tests/test_step_weights_recompute.py
git commit -m "feat(services): per-user step-weights recompute orchestration (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Config settings + worker periodic loop

**Files:**
- Modify: `vts/core/config.py` (3 settings on `Settings`)
- Modify: `vts/worker/main.py` (start the loop)
- Test: `tests/test_step_weights_loop.py`

**Interfaces:**
- Consumes: Task 4 `recompute_all_users`; `SessionLocal`; `Settings`.
- Produces: a startable background loop; settings `progress_weights_enabled: bool`, `progress_weights_recompute_interval_seconds: int`, `progress_weights_min_samples: int`.

- [ ] **Step 1: Write the failing test** (test the loop body runs one recompute then honors the stop, without real sleeping — extract the loop body into a helper that the worker calls, so it's unit-testable)

```python
# tests/test_step_weights_loop.py
import asyncio
import pytest
from vts.worker.main import _step_weights_tick
from vts.core.config import get_settings

pytestmark = pytest.mark.asyncio


async def test_tick_calls_recompute(monkeypatch):
    calls = {}

    async def fake_recompute(session_factory, *, min_samples, **kw):
        calls["min_samples"] = min_samples
        return 0

    monkeypatch.setattr("vts.worker.main.recompute_all_users", fake_recompute)
    await _step_weights_tick(min_samples=7)
    assert calls["min_samples"] == 7


async def test_settings_defaults():
    s = get_settings()
    assert s.progress_weights_enabled is True
    assert s.progress_weights_recompute_interval_seconds == 604800
    assert s.progress_weights_min_samples == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights_loop.py -v`
Expected: FAIL — `ImportError: cannot import name '_step_weights_tick'` and/or `AttributeError` on settings.

- [ ] **Step 3a: Add settings** (in `vts/core/config.py`, add to `Settings` near the other feature toggles like `night_mode_enabled`)

```python
    progress_weights_enabled: bool = True
    progress_weights_recompute_interval_seconds: int = 604800
    progress_weights_min_samples: int = 5
```

- [ ] **Step 3b: Add the loop to the worker** (in `vts/worker/main.py`)

Add imports at top:
```python
from vts.services.step_weights_recompute import recompute_all_users
```

Add these module-level helpers (after `recover_pending_tasks`):
```python
async def _step_weights_tick(*, min_samples: int) -> None:
    await recompute_all_users(SessionLocal, min_samples=min_samples)


async def _step_weights_loop() -> None:
    settings = get_settings()
    log = logging.getLogger("vts.worker")
    # Small startup jitter so a fresh deploy doesn't recompute before the
    # queue has drained; then recompute on the configured interval.
    await asyncio.sleep(5)
    while True:
        try:
            await _step_weights_tick(min_samples=settings.progress_weights_min_samples)
        except Exception:
            log.exception("step-weights loop iteration failed")
        await asyncio.sleep(settings.progress_weights_recompute_interval_seconds)
```

In `worker_loop()`, right after `pump_task = asyncio.create_task(_pump())`, start the loop when enabled:
```python
        settings_for_weights = get_settings()
        weights_task: asyncio.Task[None] | None = None
        if settings_for_weights.progress_weights_enabled:
            weights_task = asyncio.create_task(_step_weights_loop())
```
And in the worker's shutdown/cleanup (where `pump_task` is cancelled — find the `with suppress(...)` / cancel block), cancel `weights_task` too if not None:
```python
        if weights_task is not None:
            weights_task.cancel()
            with suppress(asyncio.CancelledError):
                await weights_task
```
(If the existing cleanup cancels `pump_task` in a `finally`, mirror that exactly for `weights_task`.)

- [ ] **Step 4: Run to verify pass**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights_loop.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add vts/core/config.py vts/worker/main.py tests/test_step_weights_loop.py
git commit -m "feat(worker): periodic per-user step-weights recompute loop + settings (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Endpoint + Pydantic schema + client wiring + version bump

**Files:**
- Modify: `vts/api/schemas.py` (add `ProgressWeightsOut`)
- Modify: `vts/api/main.py` (add `GET /api/progress-weights`)
- Modify: `vts/static/app.js` (loadProgressWeights + read with fallback)
- Modify: `vts/__init__.py` (version bump)
- Test: `tests/test_progress_weights_api.py` (Postgres + httpx client, mirror `tests/test_presets_api.py`)

**Interfaces:**
- Consumes: Task 3 `get_user_step_weights`; Task 1 `SEED_STEP_WEIGHTS`, `SEED_FINAL_SUMMARY_FALLBACK`; existing `get_current_user`, `get_session_dep`, `Repo`, `AuthenticatedUser`.
- Produces: `GET /api/progress-weights -> {weights: dict[str,float], final_summary_fallback: float}`.

- [ ] **Step 1: Write the failing test** (mirror auth/fixture setup of `tests/test_presets_api.py`; reuse its app/client + admin/impersonation helpers)

```python
# tests/test_progress_weights_api.py
import pytest
from datetime import datetime, timezone
from vts.db.repo import Repo
from vts.metrics.step_weights import SEED_STEP_WEIGHTS, SEED_FINAL_SUMMARY_FALLBACK

pytestmark = pytest.mark.asyncio


async def test_no_row_returns_seed(client, db_session):
    # default authed user has no user_step_weights row
    resp = await client.get("/api/progress-weights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["weights"]["transcribe_segments"] == SEED_STEP_WEIGHTS["transcribe_segments"]
    assert body["final_summary_fallback"] == SEED_FINAL_SUMMARY_FALLBACK


async def test_existing_row_returned(client, db_session, current_user_id):
    repo = Repo(db_session)
    await repo.upsert_user_step_weights(
        current_user_id, {"download": 12.3}, 321.0,
        datetime.now(tz=timezone.utc), {"download": 9},
    )
    await db_session.commit()
    resp = await client.get("/api/progress-weights")
    body = resp.json()
    assert body["weights"]["download"] == 12.3
    assert body["final_summary_fallback"] == 321.0


async def test_impersonation_returns_target_user_weights(admin_client, db_session, make_user):
    # admin acting as user X sees X's weights, not their own
    target = await make_user("target@example.com")
    repo = Repo(db_session)
    await repo.upsert_user_step_weights(
        target.id, {"download": 7.7}, 200.0, datetime.now(tz=timezone.utc), {"download": 9})
    await db_session.commit()
    resp = await admin_client.get("/api/progress-weights?as_user=target@example.com")
    assert resp.status_code == 200
    assert resp.json()["weights"]["download"] == 7.7
```

(Use whatever fixtures the existing API tests expose for authed `client`, `admin_client`, `current_user_id`, `make_user`. If names differ, adapt to `tests/conftest.py`. The three behaviors — seed when no row, stored row when present, impersonation target — are the contract; keep them.)

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_progress_weights_api.py -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 3a: Add the schema** (`vts/api/schemas.py`)

```python
class ProgressWeightsOut(BaseModel):
    weights: dict[str, float]
    final_summary_fallback: float
```

- [ ] **Step 3b: Add the endpoint** (`vts/api/main.py`, near the other per-user GETs like `/api/prompts`; add `ProgressWeightsOut` to the schemas import block and `SEED_STEP_WEIGHTS, SEED_FINAL_SUMMARY_FALLBACK` import from `vts.metrics.step_weights`)

```python
    @app.get("/api/progress-weights", response_model=ProgressWeightsOut)
    async def progress_weights_endpoint(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> ProgressWeightsOut:
        repo = Repo(session)
        row = await repo.get_user_step_weights(uuid.UUID(user.id))
        if row is not None and isinstance(row.weights, dict) and row.weights:
            fallback = row.final_summary_fallback
            return ProgressWeightsOut(
                weights={k: float(v) for k, v in row.weights.items()},
                final_summary_fallback=float(fallback) if fallback is not None else SEED_FINAL_SUMMARY_FALLBACK,
            )
        return ProgressWeightsOut(
            weights=dict(SEED_STEP_WEIGHTS),
            final_summary_fallback=SEED_FINAL_SUMMARY_FALLBACK,
        )
```

- [ ] **Step 3c: Wire the client** (`vts/static/app.js`)

Add module-level state near the constants (after line ~85):
```javascript
let serverStepWeights = null;
let serverFinalFallback = null;
```
Add the loader (near `loadPushConfig`):
```javascript
async function loadProgressWeights() {
  try {
    const data = await api("/api/progress-weights");
    if (data && data.weights && typeof data.weights === "object") {
      serverStepWeights = data.weights;
      serverFinalFallback = Number.isFinite(Number(data.final_summary_fallback))
        ? Number(data.final_summary_fallback)
        : null;
    }
  } catch {
    // keep nulls -> getStepWeight falls back to hardcoded STEP_WEIGHT_SECONDS
  }
}
```
Change `estimateFinalSummaryWeight` (currently lines ~783-790) to prefer server values.

**Important scale note for the implementer:** the seed/server `summarize_windows` value is now "seconds per REAL window". The OLD code computed `STEP_WEIGHT_SECONDS.summarize_windows / (total-1)` because the old constant was "per whole step". Since the value is now already per-window, the per-final-summary weight is simply that per-window value (one final-summary call ≈ one window). Use exactly this body:
```javascript
function estimateFinalSummaryWeight(runtime) {
  const summaryTotal = Number(runtime && runtime.summary ? runtime.summary.total : 0);
  const hasWindows = Number.isFinite(summaryTotal) && summaryTotal > 1;
  const perWindow = (serverStepWeights && Number.isFinite(Number(serverStepWeights.summarize_windows)))
    ? Number(serverStepWeights.summarize_windows)
    : STEP_WEIGHT_SECONDS.summarize_windows;
  if (hasWindows) {
    return perWindow;
  }
  return Number.isFinite(serverFinalFallback) ? serverFinalFallback : FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS;
}
```
ALSO update `STEP_WEIGHT_SECONDS.summarize_windows` in `app.js` from `598.4` to `74.8` so the offline fallback is in the same per-window scale (search the constants block, change only that one value; leave the other constants).
Change `getStepWeight` (lines ~792-803) to read server weights first:
```javascript
function getStepWeight(runtime, stepName) {
  if (stepName === "summarize_final" || stepName.startsWith("finalize:")) {
    return estimateFinalSummaryWeight(runtime);
  }
  const serverVal = serverStepWeights ? Number(serverStepWeights[stepName]) : NaN;
  if (Number.isFinite(serverVal) && serverVal > 0) {
    return serverVal;
  }
  const value = STEP_WEIGHT_SECONDS[stepName];
  if (Number.isFinite(value) && value > 0) {
    return value;
  }
  return 1;
}
```
Call the loader in `bootstrap()` after `loadPushConfig()`:
```javascript
  await loadPushConfig();
  await loadProgressWeights();
```

- [ ] **Step 3d: Bump version** — `vts/__init__.py`: `__version__ = "1.1.13"` → `"1.1.14"`.

- [ ] **Step 4: Verify**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_progress_weights_api.py -v`
Expected: PASS (3 passed).
Run: `node --check vts/static/app.js && echo "JS OK"`
Expected: `JS OK`.

- [ ] **Step 5: Commit**

```bash
git add vts/api/schemas.py vts/api/main.py vts/static/app.js vts/__init__.py tests/test_progress_weights_api.py
git commit -m "feat(api): GET /api/progress-weights + client fetch with offline fallback (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: UI verifier scenario + full-suite gate

**Files:**
- Create: `tests/ui/scenarios/progress-weights.mjs`
- Reference: `tests/ui/harness.mjs`, `tests/ui/run.mjs`, existing scenarios (e.g. `tests/ui/scenarios/smoke-boot.mjs`) for the pattern.

**Interfaces:** Consumes the running static frontend via the verifier harness; stubs `/api/progress-weights`.

- [ ] **Step 1: Write the scenario** (black-box: boot with a stubbed weights endpoint, assert the app boots and shows version — i.e. `loadProgressWeights` doesn't break bootstrap; then a 500 case proving the fallback path keeps the app working)

```javascript
// tests/ui/scenarios/progress-weights.mjs
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "progress-weights";

export async function run() {
  const failures = [];

  // Case 1: endpoint returns server weights -> app boots normally.
  {
    const { server, baseUrl } = await startStubServer({
      "/api/progress-weights": {
        weights: {
          download: 5.5, extract_audio: 2.0, trim_initial_silence: 0.3,
          segment_audio: 1.2, detect_language: 2.6, transcribe_segments: 174.8,
          merge_transcript: 0.1, prepare_llama_model: 6.3, prepare_summary_chunks: 0.1,
          summarize_windows: 74.8,
        },
        final_summary_fallback: 514.4,
      },
    });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      if (!(await isVisible(page, "#app-version"))) {
        failures.push("case1: app did not boot (version label missing)");
      }
      if (errors.length) failures.push(`case1: console errors: ${errors.join("; ")}`);
    } finally {
      await browser.close();
      server.close();
    }
  }

  // Case 2: endpoint 500 -> client falls back to hardcoded constants, app still boots.
  {
    const { server, baseUrl } = await startStubServer({
      "/api/progress-weights": { __status: 500 },
    });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      if (!(await isVisible(page, "#app-version"))) {
        failures.push("case2: app did not boot on weights 500 (fallback broken)");
      }
      if (errors.length) failures.push(`case2: console errors: ${errors.join("; ")}`);
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}
```
**Note for implementer:** check `tests/ui/harness.mjs` for how `startStubServer` overrides are shaped and how to force a non-200 (the `{ __status: 500 }` form is a guess — match the harness's actual mechanism; if it only supports JSON bodies, simulate failure by overriding with a handler that returns 500, or by the harness's documented way). If the harness cannot return 500, drop case 2 and instead assert case 1 only, and log that the fallback path is covered by the JS unit behavior. Do NOT invent a harness API.

- [ ] **Step 2: Run the verifier**

Run: `cd /home/victor/dev/vts/tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`, exit 0, with `progress-weights` among the PASS lines.

- [ ] **Step 3: Run the relevant Python suite** (all new + touched tests)

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_step_weights.py tests/test_user_step_weights_migration.py tests/test_user_step_weights_repo.py tests/test_step_weights_recompute.py tests/test_step_weights_loop.py tests/test_progress_weights_api.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/ui/scenarios/progress-weights.mjs
git commit -m "test(ui): progress-weights boot + fallback verifier scenario (vts-8cm)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Хранение / таблица `user_step_weights` + миграция 0012 → Task 2. ✓
- Сбор длительностей из steps/tasks без схемы → Task 3 `step_durations_for_user`. ✓
- `aggregate_step_weights(window_offset)`, `step_sample_counts`, `merge_with_seed`, `final_summary_fallback`, `SEED_*` → Task 1. ✓
- Per-step порог min_samples (дефолт 5) → Task 1 `merge_with_seed` + Task 4 wiring + tests. ✓
- Сервис recompute_for_user / recompute_all_users, per-user try/except → Task 4. ✓
- Worker фоновый цикл + конфиг (interval/min_samples/enabled, дефолты) → Task 5. ✓
- Endpoint `GET /api/progress-weights`, всегда полный набор (row → seed) → Task 6. ✓
- Имперсонация: scope по `user.id`, не `requested_by`; тест admin as_user → Task 6 endpoint + test. ✓
- Recompute attribution by tasks.user_id (worker, no request ctx) → Task 4 (queries by user_id) — inherent. ✓
- Клиент loadProgressWeights в bootstrap + фолбэк на хардкод; шкала на реальное окно; seed app.js → 74.8 → Task 6 (3c). ✓
- Window convention total-1 → Task 1 (offset) + Task 4 (`_WINDOW_OFFSET=1`) + Task 6 client. ✓
- Версия bump → Task 6. ✓
- Тесты (metrics, recompute, api incl impersonation, UI verifier incl fallback) → Tasks 1,3,4,6,7. ✓

**Placeholder scan:** No TBD/TODO. Task 7 contains an explicit "match the harness's actual mechanism" instruction with a fallback if 500 isn't supported — that's a guarded real instruction, not a placeholder (the harness API genuinely must be read at implementation time; the contract — boot succeeds, fallback works — is concrete). Task 6 (3c) gives a single, unambiguous `estimateFinalSummaryWeight` body (the earlier confusing draft was removed) preceded by the scale note explaining why the per-window value is used directly.

**Type consistency:** `StepDuration(name, duration_sec, window_total)` consistent across Tasks 1/3/4. `aggregate_step_weights(..., window_offset=)`, `step_sample_counts`, `merge_with_seed`, `final_summary_fallback`, `SEED_STEP_WEIGHTS`, `SEED_FINAL_SUMMARY_FALLBACK` identical in Tasks 1/4/6. Repo methods `step_durations_for_user`/`upsert_user_step_weights`/`get_user_step_weights`/`users_with_completed_tasks` identical in Tasks 3/4/6. `ProgressWeightsOut(weights, final_summary_fallback)` consistent Task 6 schema↔endpoint↔test. Settings names `progress_weights_enabled/_recompute_interval_seconds/_min_samples` consistent Tasks 5↔config↔worker.
