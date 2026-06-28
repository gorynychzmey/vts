# Step-Weights Recompute Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale hardcoded `STEP_WEIGHT_SECONDS` progress-bar weights in `app.js` with values recomputed (one-off) from real durations of completed steps in completed tasks.

**Architecture:** A pure, DB-free aggregation function (`vts/metrics/step_weights.py`) computes per-step median durations from a list of step-duration rows, normalizing `summarize_windows` per-window. A thin script (`scripts/recompute_step_weights.py`) queries Postgres for completed steps of completed tasks, feeds the rows to the pure function, and prints a ready-to-paste JS block. A human pastes the numbers into `app.js`. No schema change, no new API.

**Tech Stack:** Python 3, SQLAlchemy async (existing `vts.db.session`), pytest. Client: vanilla JS (`vts/static/app.js`).

## Global Constraints

- Self-hosted / on-prem: no external services, no network calls at runtime — copied verbatim from project CLAUDE.md.
- Bump `vts/__init__.py` `__version__` before committing client-facing changes (the `app.js` number change is client-facing).
- `app.js` has no `defer`: any new DOM block referenced via `getElementById` must precede the `<script>` tag. (Not triggered by this plan — no new DOM — noted for safety.)
- Robust statistic is **median**, not mean. Only `status=completed` steps of `status=completed` tasks count.

---

### Task 1: Pure aggregation function `aggregate_step_weights`

**Files:**
- Create: `vts/metrics/step_weights.py`
- Test: `tests/test_step_weights.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `StepDuration = namedtuple("StepDuration", ["name", "duration_sec", "window_total"])` where `name: str`, `duration_sec: float`, `window_total: int | None`.
  - `aggregate_step_weights(rows: list[StepDuration]) -> dict[str, float]` — returns `{step_name: median_seconds}`. For `summarize_windows`, the value is the median of `duration_sec / window_total` over rows whose `window_total >= 1` (i.e. **per-window** seconds); rows with `window_total` missing/`< 1` are skipped for that step only. Steps with no usable rows are absent from the result. Values rounded to 1 decimal.
  - `median(values: list[float]) -> float` — helper (linear-interpolation-free: lower-middle average for even counts). Exposed for testing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_step_weights.py
from vts.metrics.step_weights import StepDuration, aggregate_step_weights, median


def test_median_odd_and_even():
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_fixed_step_uses_median_duration():
    rows = [
        StepDuration("download", 10.0, None),
        StepDuration("download", 20.0, None),
        StepDuration("download", 30.0, None),
    ]
    assert aggregate_step_weights(rows) == {"download": 20.0}


def test_summarize_windows_normalized_per_window():
    # durations 100 over 10 windows -> 10/window; 60 over 6 -> 10/window
    rows = [
        StepDuration("summarize_windows", 100.0, 10),
        StepDuration("summarize_windows", 60.0, 6),
    ]
    assert aggregate_step_weights(rows) == {"summarize_windows": 10.0}


def test_summarize_windows_skips_rows_without_window_total():
    rows = [
        StepDuration("summarize_windows", 100.0, 10),  # 10/window
        StepDuration("summarize_windows", 999.0, 0),   # skipped (total < 1)
        StepDuration("summarize_windows", 999.0, None), # skipped
    ]
    assert aggregate_step_weights(rows) == {"summarize_windows": 10.0}


def test_step_with_no_rows_absent_from_result():
    rows = [StepDuration("download", 5.0, None)]
    result = aggregate_step_weights(rows)
    assert "extract_audio" not in result


def test_outlier_does_not_move_median_much():
    rows = [StepDuration("extract_audio", v, None) for v in (6.0, 7.0, 8.0, 6.5, 9000.0)]
    # median of 5 sorted values -> the middle one (7.0), outlier ignored
    assert aggregate_step_weights(rows) == {"extract_audio": 7.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/victor/dev/vts && python -m pytest tests/test_step_weights.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.metrics.step_weights'`

- [ ] **Step 3: Write minimal implementation**

```python
# vts/metrics/step_weights.py
"""Pure aggregation of per-step progress-bar weights from run durations.

DB-free and deterministic so both the one-off recompute script (vts-b6t) and
the per-user followup (vts-8cm) can reuse it. Median, not mean, for robustness.
"""
from __future__ import annotations

from collections import namedtuple

StepDuration = namedtuple("StepDuration", ["name", "duration_sec", "window_total"])

# Step whose duration scales with the number of summary windows; stored per-window.
_PER_WINDOW_STEP = "summarize_windows"


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def aggregate_step_weights(rows: list[StepDuration]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            total = row.window_total
            if not isinstance(total, int) or total < 1:
                continue
            buckets.setdefault(row.name, []).append(row.duration_sec / total)
        else:
            buckets.setdefault(row.name, []).append(row.duration_sec)
    return {name: round(median(vals), 1) for name, vals in buckets.items() if vals}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/victor/dev/vts && python -m pytest tests/test_step_weights.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add vts/metrics/step_weights.py tests/test_step_weights.py
git commit -m "feat(metrics): pure aggregate_step_weights (median, per-window summarize) (vts-b6t)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Recompute script `scripts/recompute_step_weights.py`

**Files:**
- Create: `scripts/recompute_step_weights.py`
- Reference (read, do not modify): `vts/db/session.py` (`get_db_session_factory`), `vts/db/models.py` (`Task`, `Step`, `TaskStatus`, `StepStatus`), `vts/services/task_progress.py` (`summary_progress_for_task`), `vts/static/app.js:72-85` (target block format).

**Interfaces:**
- Consumes: `aggregate_step_weights`, `StepDuration`, `median` from Task 1; `get_db_session_factory()` returning an `async_sessionmaker[AsyncSession]`.
- Produces: stdout only — a JS block matching the `app.js` shape plus per-step sample counts. No return value relied on by other tasks.

**Why no unit test:** thin I/O wrapper over the tested pure function; verified by a real run against a DB dump/prod during recalibration. (Per spec.)

- [ ] **Step 1: Write the script**

```python
# scripts/recompute_step_weights.py
"""One-off recompute of progress-bar step weights from completed runs (vts-b6t).

Queries completed steps of completed tasks, computes median per-step durations
(summarize_windows normalized per window), and prints a ready-to-paste block for
vts/static/app.js. Does NOT edit app.js — paste the numbers by hand.

Run:  python -m scripts.recompute_step_weights
Needs the same DB config as the app (database_url / VTS_* env).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from vts.db.models import Step, StepStatus, Task, TaskStatus
from vts.db.session import get_db_session_factory
from vts.metrics.step_weights import StepDuration, aggregate_step_weights, median
from vts.services.task_progress import summary_progress_for_task

# Fixed-order steps mirrored from app.js STEP_WEIGHT_SECONDS for stable output.
_PRINT_ORDER = [
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
]


async def _collect_rows() -> tuple[list[StepDuration], list[int]]:
    factory = get_db_session_factory()
    rows: list[StepDuration] = []
    window_totals: list[int] = []
    async with factory() as session:
        result = await session.execute(
            select(Task).where(Task.status == TaskStatus.completed)
        )
        tasks = result.scalars().all()
        for task in tasks:
            _current, total = summary_progress_for_task(task)
            window_total = total if total >= 1 else None
            if window_total is not None:
                window_totals.append(window_total)
            steps_result = await session.execute(
                select(Step).where(
                    Step.task_id == task.id, Step.status == StepStatus.completed
                )
            )
            for step in steps_result.scalars().all():
                if step.started_at is None or step.finished_at is None:
                    continue
                duration = (step.finished_at - step.started_at).total_seconds()
                if duration < 0:
                    continue
                rows.append(StepDuration(step.name, duration, window_total))
    return rows, window_totals


def _count(rows: list[StepDuration], name: str) -> int:
    if name == "summarize_windows":
        return sum(1 for r in rows if r.name == name and isinstance(r.window_total, int) and r.window_total >= 1)
    return sum(1 for r in rows if r.name == name)


def _final_summary_fallback(rows: list[StepDuration]) -> float | None:
    finals = [r.duration_sec for r in rows if r.name == "summarize_final"]
    return round(median(finals), 1) if finals else None


def main() -> None:
    rows, window_totals = asyncio.run(_collect_rows())
    weights = aggregate_step_weights(rows)

    # summarize_windows is stored per-window; app.js expects "per whole step"
    # (it divides by window count at runtime). Reconstruct a representative
    # per-step value using the median window count across runs.
    typical_windows = int(median([float(w) for w in window_totals])) if window_totals else 0
    per_window = weights.get("summarize_windows")
    summarize_windows_per_step = (
        round(per_window * typical_windows, 1)
        if per_window is not None and typical_windows >= 1
        else None
    )

    fallback = _final_summary_fallback(rows)

    print("// --- paste into vts/static/app.js (STEP_WEIGHT_SECONDS) ---")
    print("const STEP_WEIGHT_SECONDS = {")
    for name in _PRINT_ORDER:
        if name == "summarize_windows":
            val = summarize_windows_per_step
        else:
            val = weights.get(name)
        n = _count(rows, name)
        if val is None:
            print(f"  // {name}: NO DATA (n=0) — keep existing value")
            continue
        comma = "," if name != _PRINT_ORDER[-1] else ""
        print(f"  {name}: {val}{comma}  // n={n}")
    print("};")
    if summarize_windows_per_step is not None:
        print(
            f"// summarize_windows per-window median = {per_window} s "
            f"(typical {typical_windows} windows -> {summarize_windows_per_step} s/step)"
        )
    if fallback is not None:
        n_final = sum(1 for r in rows if r.name == "summarize_final")
        print(f"const FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS = {fallback};  // n={n_final}")
    else:
        print("// FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS: NO DATA — keep existing value")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check it imports and has no syntax errors**

Run: `cd /home/victor/dev/vts && python -c "import ast; ast.parse(open('scripts/recompute_step_weights.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/recompute_step_weights.py
git commit -m "feat(scripts): recompute_step_weights prints app.js block from completed runs (vts-b6t)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Run recompute against real data and paste numbers into `app.js`

**Files:**
- Modify: `vts/static/app.js:72-85` (the `STEP_WEIGHT_SECONDS` object + `FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS`) — numbers only.
- Modify: `vts/__init__.py` (version bump).

**Interfaces:**
- Consumes: the script from Task 2. No new code.

**Note:** This task requires DB access (prod or a dump). If unavailable in the execution environment, STOP and hand back to the user with the script ready — the user runs it and supplies the numbers. Do NOT invent numbers.

- [ ] **Step 1: Run the script against the configured DB**

Run: `cd /home/victor/dev/vts && python -m scripts.recompute_step_weights`
Expected: a `const STEP_WEIGHT_SECONDS = { ... }` block with `// n=<count>` per line. If every line says `NO DATA`, the DB env is wrong — fix `database_url`/`VTS_*` env before continuing.

- [ ] **Step 2: Paste the numbers into `app.js`**

Replace ONLY the numeric values in the `STEP_WEIGHT_SECONDS` object (lines 72-83) and `FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS` (line 85) with the script output. For any step the script reports as `NO DATA`, leave the existing value untouched. Keep the keys, order, and comments. Update the comment on line 84 to note re-measurement date, e.g. `// Fallback = median summarize_final over completed runs (recomputed 2026-06-28).`

- [ ] **Step 3: Verify app.js still parses**

Run: `cd /home/victor/dev/vts && node --check vts/static/app.js && echo "JS OK"`
Expected: `JS OK`

- [ ] **Step 4: Bump version**

Edit `vts/__init__.py`: increment the patch version of `__version__`.

- [ ] **Step 5: Commit**

```bash
git add vts/static/app.js vts/__init__.py
git commit -m "chore(progress): recalibrate STEP_WEIGHT_SECONDS from completed runs (vts-b6t)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Browser verify + close

**Files:** none (verification only).

**Interfaces:** Consumes the running static frontend via the `verifier-web` skill.

- [ ] **Step 1: Run the UI verifier (app.js changed)**

Run: `cd /home/victor/dev/vts/tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`, exit 0. (Weights affect progress math only; smoke set must stay green — no regression in boot/dialogs.)

- [ ] **Step 2: Run the full Python test suite for the metrics module**

Run: `cd /home/victor/dev/vts && python -m pytest tests/test_step_weights.py -v`
Expected: PASS.

- [ ] **Step 3: Close the bd issue and push**

```bash
cd /home/victor/dev/vts
bd close vts-b6t --reason="Recomputed STEP_WEIGHT_SECONDS from completed runs via scripts/recompute_step_weights.py + pure aggregate_step_weights (median, per-window). app.js recalibrated, UI verifier green."
bd dolt push
git push
git status   # must show up to date with origin
```

---

## Self-Review

**Spec coverage:**
- One-off recompute over completed steps of completed tasks → Task 2 query + Task 3 run. ✓
- Median, robust → Task 1 `median` + tests. ✓
- `summarize_windows` per-window normalization → Task 1 `_PER_WINDOW_STEP` branch + test `test_summarize_windows_normalized_per_window`. ✓
- `FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS` = median `summarize_final` → Task 2 `_final_summary_fallback`. ✓
- Empty/scarce input → step skipped, old value kept → Task 1 (absent from result) + Task 3 Step 2 ("leave existing value"). ✓
- Pure function reused by followup → Task 1 module, DB-free. ✓
- Client-scale decision (per-window vs per-step) → resolved in Task 2: script reconstructs per-step value via median window count, `app.js` runtime logic untouched. ✓
- Tests file `tests/test_step_weights.py` → Task 1. ✓
- New files `vts/metrics/step_weights.py`, `scripts/recompute_step_weights.py`; modify `app.js`; no schema/API change → matches spec "Изменения в коде (итог)". ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. Task 3 numbers are intentionally produced at runtime (one-off recompute) with an explicit STOP-and-handback guard, not a placeholder.

**Type consistency:** `StepDuration(name, duration_sec, window_total)`, `aggregate_step_weights`, `median` used identically in Tasks 1 and 2. `get_db_session_factory()`, `summary_progress_for_task` match the verified source. `TaskStatus.completed` / `StepStatus.completed` match `vts/db/models.py`.
