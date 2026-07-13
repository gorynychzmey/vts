# Parallel Task Processing (Lanes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Worker processes several tasks concurrently by scheduling pipeline steps into resource lanes (network / ffmpeg / GPU with asr>llm priority), with a new `waiting` task status and per-lane queue positions in API/UI.

**Architecture:** Single worker process keeps up to N `process_task` coroutines in flight. A new in-process `LaneManager` arbitrates slots: network (1) and ffmpeg (2) are acquired per step in `_run_step`; the GPU lane (1 slot) replaces the 7 `async with self.heavy_slot:` call sites inside step methods, with two priority classes (asr beats llm, anti-starvation burst=3) and night-mode gating. Lane queues are mirrored to Redis for the API. Spec: `docs/superpowers/specs/2026-07-13-parallel-task-processing-design.md`.

**Tech Stack:** Python 3.12 asyncio, SQLAlchemy async + alembic, Redis (redis.asyncio), FastAPI, vanilla JS frontend, pytest + pytest-asyncio (Postgres test engine via `tests/_db.py`).

## Global Constraints

- Task tracking: **bd only** (issue vts-rhs) — no TodoWrite/markdown TODO.
- Commit after every task; push at session end (`git pull --rebase && git push`).
- Version bump in `vts/__init__.py` happens ONCE, in the final task (not per commit; docs/spec-only commits never bump).
- New settings (exact names): `worker_max_active_tasks=4`, `lane_network_slots=1`, `lane_ffmpeg_slots=2`, `lane_gpu_slots=1`, `gpu_asr_burst=3` (env: `VTS_WORKER_MAX_ACTIVE_TASKS` etc.).
- `heavy_slot_limit` setting, `vts/services/heavy_slot.py`, and the worker's `heavy_slots` Redis reset are removed by the end (Task 10).
- New task status literal: `waiting`. Migration id: `0013_task_status_waiting`.
- Redis lane snapshot key: `{redis_prefix}queue:lanes`, TTL 10 s, JSON `{"network": [task_id…], "ffmpeg": […], "gpu_asr": […], "gpu_llm": […]}`.
- Test suite must stay green after every task: `pytest -q`.

---

### Task 1: Settings for lanes and worker pool

**Model:** Sonnet 5 — exact fields and file locations named, pattern to copy is adjacent.

**Files:**
- Modify: `vts/core/config.py` (fields near `heavy_slot_limit`, ~line 119)
- Modify: `config.yaml` (new sections near `heavy_slot:`, ~line 78)
- Test: `tests/test_config_yaml.py`

**Interfaces:**
- Produces: `Settings.worker_max_active_tasks: int = 4`, `Settings.lane_network_slots: int = 1`, `Settings.lane_ffmpeg_slots: int = 2`, `Settings.lane_gpu_slots: int = 1`, `Settings.gpu_asr_burst: int = 3`. All later tasks read these.

- [ ] **Step 1: Write the failing test** — in `tests/test_config_yaml.py`, next to the existing `heavy_slot` yaml-override assertions (see lines ~111 and ~161), add:

```python
def test_lane_settings_defaults():
    from vts.core.config import Settings
    s = Settings()
    assert s.worker_max_active_tasks == 4
    assert s.lane_network_slots == 1
    assert s.lane_ffmpeg_slots == 2
    assert s.lane_gpu_slots == 1
    assert s.gpu_asr_burst == 3
```

Also extend the existing yaml-override test dict (the one containing `"heavy_slot": {"limit": 2}`) with `"worker": {"max_active_tasks": 2}, "lane": {"gpu_slots": 2}, "gpu": {"asr_burst": 5}` and assert `settings.worker_max_active_tasks == 2`, `settings.lane_gpu_slots == 2`, `settings.gpu_asr_burst == 5` next to the `heavy_slot_limit` assertion.

- [ ] **Step 2: Run** `pytest tests/test_config_yaml.py -q` — expect FAIL (unknown attribute).
- [ ] **Step 3: Implement** — in `vts/core/config.py` directly under `heavy_slot_limit: int = 1` add:

```python
    worker_max_active_tasks: int = 4
    lane_network_slots: int = 1
    lane_ffmpeg_slots: int = 2
    lane_gpu_slots: int = 1
    gpu_asr_burst: int = 3
```

In `config.yaml`, after the `heavy_slot:` block add (values = defaults, commented purpose per key):

```yaml
worker:
  max_active_tasks: 4   # tasks in flight; 1 ≈ legacy sequential behaviour
lane:
  network_slots: 1      # concurrent downloads
  ffmpeg_slots: 2       # concurrent ffmpeg jobs
  gpu_slots: 1          # concurrent GPU calls (Whisper+LLM share)
gpu:
  asr_burst: 3          # max consecutive ASR grants while LLM waits
```

- [ ] **Step 4: Run** `pytest tests/test_config_yaml.py -q` — expect PASS.
- [ ] **Step 5: Commit** `feat(config): lane and worker-pool settings (vts-rhs)`

---

### Task 2: LaneManager

**Model:** Opus 4.8 — subtle asyncio scheduling logic (priority, starvation, cancellation, night gating).

**Files:**
- Create: `vts/worker/lanes.py`
- Test: `tests/test_lane_manager.py` (new)

**Interfaces:**
- Consumes: `Settings` fields from Task 1.
- Produces (used by Tasks 5–8):

```python
class LaneManager:
    def __init__(self, settings: Settings, *,
                 night_allowed: Callable[[], bool] | None = None,
                 on_change: Callable[[dict[str, list[str]]], Awaitable[None]] | None = None) -> None: ...
    def slot(self, lane: str, task_id: uuid.UUID, cls: str = "main", *,
             on_wait: Callable[[], Awaitable[None]] | None = None,
             on_grant: Callable[[], Awaitable[None]] | None = None): ...  # async context manager
    def snapshot(self) -> dict[str, list[str]]:  # {"network": [...], "ffmpeg": [...], "gpu_asr": [...], "gpu_llm": [...]}
```

Semantics (all must hold, each is a test):
1. Lane names: `network`, `ffmpeg`, `gpu`; slot counts from settings. `cls` is only meaningful for `gpu` (`"asr"`/`"llm"`); other lanes use `"main"`.
2. Immediate grant (free slot, no queued waiter that outranks, night allows) does NOT call `on_wait`/`on_grant`.
3. Otherwise the caller is enqueued FIFO within its class; `on_wait` fires once at enqueue, `on_grant` once at grant.
4. GPU release grants oldest `asr` waiter first, else oldest `llm`; after `gpu_asr_burst` consecutive asr grants with a non-empty llm queue, grant llm and reset the streak; any llm grant resets the streak.
5. `asyncio.CancelledError` while waiting removes the waiter from its queue and re-raises; the slot is not leaked.
6. Exception inside the `async with` body still releases the slot (`__aexit__`).
7. `night_allowed()` (default: computed from `settings.night_mode_*` with the same hour-window formula as the current `HeavySlot._wait_night_mode`, [vts/services/heavy_slot.py:17-27](../../vts/services/heavy_slot.py)) gates ONLY the `gpu` lane. When it returns False, no gpu grant happens; a 30 s `loop.call_later` retry is scheduled whenever gpu waiters exist so waiters wake when the window opens (no deadlock).
8. `on_change(snapshot)` is awaited after every enqueue/grant/release/cancel-removal; `snapshot()` lists waiting task_ids as strings in queue order (a task_id may legitimately appear twice in `gpu_asr` when `transcribe_parallel_per_task > 1`).

- [ ] **Step 1: Write failing tests** — `tests/test_lane_manager.py`. Core cases (all `@pytest.mark.asyncio`, build `Settings()` with `monkeypatch` or pass a `SimpleNamespace` with the five fields + `night_mode_enabled=False`):

```python
import asyncio, uuid
import pytest
from types import SimpleNamespace
from vts.worker.lanes import LaneManager

def _settings(**over):
    base = dict(worker_max_active_tasks=4, lane_network_slots=1, lane_ffmpeg_slots=2,
                lane_gpu_slots=1, gpu_asr_burst=3,
                night_mode_enabled=False, night_mode_start_hour=22, night_mode_end_hour=7)
    base.update(over)
    return SimpleNamespace(**base)

@pytest.mark.asyncio
async def test_immediate_grant_skips_callbacks():
    mgr = LaneManager(_settings())
    called = []
    async def on_wait(): called.append("wait")
    async with mgr.slot("network", uuid.uuid4(), on_wait=on_wait):
        pass
    assert called == []

@pytest.mark.asyncio
async def test_fifo_within_lane():
    mgr = LaneManager(_settings())
    order = []
    async def hold(tid, delay):
        async with mgr.slot("network", tid):
            order.append(tid)
            await asyncio.sleep(delay)
    t1, t2, t3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tasks = [asyncio.create_task(hold(t, 0.01)) for t in (t1, t2, t3)]
    await asyncio.gather(*tasks)
    assert order == [t1, t2, t3]

@pytest.mark.asyncio
async def test_gpu_asr_beats_llm():
    mgr = LaneManager(_settings())
    order = []
    async def use(cls, tag):
        async with mgr.slot("gpu", uuid.uuid4(), cls):
            order.append(tag)
            await asyncio.sleep(0.01)
    first = asyncio.create_task(use("llm", "llm-holder"))
    await asyncio.sleep(0.001)  # holder occupies the slot
    q_llm = asyncio.create_task(use("llm", "llm-2"))
    await asyncio.sleep(0.001)
    q_asr = asyncio.create_task(use("asr", "asr-1"))  # enqueued after llm-2, must be granted first
    await asyncio.gather(first, q_llm, q_asr)
    assert order == ["llm-holder", "asr-1", "llm-2"]

@pytest.mark.asyncio
async def test_asr_burst_yields_to_llm():
    mgr = LaneManager(_settings(gpu_asr_burst=2))
    order = []
    async def use(cls, tag):
        async with mgr.slot("gpu", uuid.uuid4(), cls):
            order.append(tag)
            await asyncio.sleep(0.005)
    holder = asyncio.create_task(use("llm", "h"))
    await asyncio.sleep(0.001)
    llm_w = asyncio.create_task(use("llm", "llm-w"))
    asr = [asyncio.create_task(use("asr", f"a{i}")) for i in range(3)]
    await asyncio.gather(holder, llm_w, *asr)
    # 2 asr grants, then forced llm, then remaining asr
    assert order == ["h", "a0", "a1", "llm-w", "a2"]

@pytest.mark.asyncio
async def test_cancel_removes_waiter():
    mgr = LaneManager(_settings())
    tid_holder, tid_wait = uuid.uuid4(), uuid.uuid4()
    entered = asyncio.Event()
    async def holder():
        async with mgr.slot("network", tid_holder):
            entered.set()
            await asyncio.sleep(0.05)
    h = asyncio.create_task(holder())
    await entered.wait()
    async def waiter():
        async with mgr.slot("network", tid_wait):
            pass
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.001)
    assert mgr.snapshot()["network"] == [str(tid_wait)]
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert mgr.snapshot()["network"] == []
    await h

@pytest.mark.asyncio
async def test_night_mode_blocks_gpu_and_retries():
    allowed = {"v": False}
    mgr = LaneManager(_settings(), night_allowed=lambda: allowed["v"])
    got = asyncio.Event()
    async def use():
        async with mgr.slot("gpu", uuid.uuid4(), "llm"):
            got.set()
    t = asyncio.create_task(use())
    await asyncio.sleep(0.01)
    assert not got.is_set()
    allowed["v"] = True
    mgr.poke()  # test hook: force re-evaluation instead of waiting 30s
    await asyncio.wait_for(got.wait(), 1)
    await t

@pytest.mark.asyncio
async def test_on_change_snapshots():
    snaps = []
    async def on_change(s): snaps.append(s)
    mgr = LaneManager(_settings(), on_change=on_change)
    async with mgr.slot("ffmpeg", uuid.uuid4()):
        pass
    assert snaps  # at least grant + release
    assert set(snaps[-1].keys()) == {"network", "ffmpeg", "gpu_asr", "gpu_llm"}
```

- [ ] **Step 2: Run** `pytest tests/test_lane_manager.py -q` — expect FAIL (module missing).
- [ ] **Step 3: Implement `vts/worker/lanes.py`.** Reference implementation (adapt freely, keep semantics):

```python
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

_LANES = ("network", "ffmpeg", "gpu")


def _default_night_allowed(settings: Any) -> Callable[[], bool]:
    def allowed() -> bool:
        if not settings.night_mode_enabled:
            return True
        now_hour = datetime.now().hour
        start = settings.night_mode_start_hour
        end = settings.night_mode_end_hour
        return (start <= now_hour) or (now_hour < end) if start > end else start <= now_hour < end
    return allowed


class _Waiter:
    __slots__ = ("task_id", "future")

    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()


class LaneManager:
    """In-process slot scheduler for pipeline resource lanes.

    gpu lane has two FIFO classes: asr (priority) and llm, with an
    anti-starvation burst limit; night mode gates gpu grants only.
    """

    def __init__(self, settings, *, night_allowed=None, on_change=None) -> None:
        self._slots = {
            "network": max(settings.lane_network_slots, 1),
            "ffmpeg": max(settings.lane_ffmpeg_slots, 1),
            "gpu": max(settings.lane_gpu_slots, 1),
        }
        self._active = {name: 0 for name in _LANES}
        self._queues: dict[tuple[str, str], deque[_Waiter]] = {
            ("network", "main"): deque(),
            ("ffmpeg", "main"): deque(),
            ("gpu", "asr"): deque(),
            ("gpu", "llm"): deque(),
        }
        self._asr_streak = 0
        self._burst = max(settings.gpu_asr_burst, 1)
        self._night_allowed = night_allowed or _default_night_allowed(settings)
        self._on_change = on_change
        self._night_timer: asyncio.TimerHandle | None = None

    # -- public -----------------------------------------------------------

    def slot(self, lane: str, task_id: uuid.UUID, cls: str = "main", *,
             on_wait=None, on_grant=None) -> "_SlotContext":
        if lane not in _LANES:
            raise ValueError(f"unknown lane: {lane}")
        if lane != "gpu":
            cls = "main"
        elif cls not in ("asr", "llm"):
            raise ValueError(f"unknown gpu class: {cls}")
        return _SlotContext(self, lane, cls, task_id, on_wait, on_grant)

    def snapshot(self) -> dict[str, list[str]]:
        return {
            "network": [str(w.task_id) for w in self._queues[("network", "main")]],
            "ffmpeg": [str(w.task_id) for w in self._queues[("ffmpeg", "main")]],
            "gpu_asr": [str(w.task_id) for w in self._queues[("gpu", "asr")]],
            "gpu_llm": [str(w.task_id) for w in self._queues[("gpu", "llm")]],
        }

    def poke(self) -> None:
        """Re-evaluate pending grants (used by the night-mode retry timer and tests)."""
        for lane in _LANES:
            self._grant_pending(lane)

    # -- internals ---------------------------------------------------------

    async def _notify(self) -> None:
        if self._on_change is not None:
            await self._on_change(self.snapshot())

    def _has_waiters(self, lane: str) -> bool:
        if lane == "gpu":
            return bool(self._queues[("gpu", "asr")] or self._queues[("gpu", "llm")])
        return bool(self._queues[(lane, "main")])

    def _try_immediate(self, lane: str) -> bool:
        if self._active[lane] >= self._slots[lane]:
            return False
        if self._has_waiters(lane):
            return False  # fairness: join the queue behind existing waiters
        if lane == "gpu" and not self._night_allowed():
            self._schedule_night_retry()
            return False
        self._active[lane] += 1
        return True

    def _pick_gpu_queue(self) -> deque[_Waiter] | None:
        asr_q = self._queues[("gpu", "asr")]
        llm_q = self._queues[("gpu", "llm")]
        if asr_q and llm_q and self._asr_streak >= self._burst:
            return llm_q
        if asr_q:
            return asr_q
        if llm_q:
            return llm_q
        return None

    def _grant_pending(self, lane: str) -> None:
        while self._active[lane] < self._slots[lane]:
            if lane == "gpu":
                if not self._night_allowed():
                    self._schedule_night_retry()
                    return
                queue = self._pick_gpu_queue()
                if queue is None:
                    return
                waiter = queue.popleft()
                if queue is self._queues[("gpu", "asr")]:
                    self._asr_streak += 1
                else:
                    self._asr_streak = 0
            else:
                queue = self._queues[(lane, "main")]
                if not queue:
                    return
                waiter = queue.popleft()
            self._active[lane] += 1
            if not waiter.future.done():
                waiter.future.set_result(None)

    def _schedule_night_retry(self) -> None:
        if self._night_timer is not None:
            return
        loop = asyncio.get_running_loop()

        def _retry() -> None:
            self._night_timer = None
            if self._has_waiters("gpu"):
                self._grant_pending("gpu")
                if self._has_waiters("gpu"):
                    self._schedule_night_retry()

        self._night_timer = loop.call_later(30, _retry)

    def _release(self, lane: str) -> None:
        self._active[lane] = max(self._active[lane] - 1, 0)
        self._grant_pending(lane)


class _SlotContext:
    def __init__(self, mgr: LaneManager, lane: str, cls: str, task_id: uuid.UUID,
                 on_wait, on_grant) -> None:
        self._mgr, self._lane, self._cls = mgr, lane, cls
        self._task_id, self._on_wait, self._on_grant = task_id, on_wait, on_grant

    async def __aenter__(self) -> "_SlotContext":
        mgr = self._mgr
        if mgr._try_immediate(self._lane):
            await mgr._notify()
            return self
        waiter = _Waiter(self._task_id)
        mgr._queues[(self._lane, self._cls)].append(waiter)
        await mgr._notify()
        if self._on_wait is not None:
            await self._on_wait()
        try:
            await waiter.future
        except asyncio.CancelledError:
            try:
                mgr._queues[(self._lane, self._cls)].remove(waiter)
            except ValueError:
                # already granted between cancel and cleanup: release the slot
                mgr._release(self._lane)
            await mgr._notify()
            raise
        await mgr._notify()
        if self._on_grant is not None:
            await self._on_grant()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._mgr._release(self._lane)
        await self._mgr._notify()
```

- [ ] **Step 4: Run** `pytest tests/test_lane_manager.py -q` — expect PASS. Also `pytest -q` (no regressions).
- [ ] **Step 5: Commit** `feat(worker): LaneManager with prioritized gpu lane (vts-rhs)`

---

### Task 3: Step→lane mapping

**Model:** Sonnet 5 — small deterministic mapping with clear home and tests.

**Files:**
- Modify: `vts/pipeline/types.py`
- Test: `tests/test_dag_tail.py` (append tests)

**Interfaces:**
- Produces: `STEP_LANES: dict[str, str]` and `lane_for_step(name: str) -> str | None` — returns `"network"` for `download`; `"ffmpeg"` for `extract_audio`/`trim_initial_silence`/`segment_audio`; `None` for every other step (GPU steps manage the lane inside their methods; `merge_transcript`/`prepare_summary_chunks` are laneless).

- [ ] **Step 1: Write failing test** (append to `tests/test_dag_tail.py`):

```python
def test_lane_for_step_mapping():
    from vts.pipeline.types import lane_for_step
    assert lane_for_step("download") == "network"
    for s in ("extract_audio", "trim_initial_silence", "segment_audio"):
        assert lane_for_step(s) == "ffmpeg"
    for s in ("detect_language", "transcribe_segments", "prepare_llama_model",
              "summarize_windows", "pack_window_notes", "summarize_final",
              "merge_transcript", "prepare_summary_chunks", "finalize:user:abc"):
        assert lane_for_step(s) is None
```

- [ ] **Step 2: Run** `pytest tests/test_dag_tail.py -q` — FAIL.
- [ ] **Step 3: Implement** in `vts/pipeline/types.py`:

```python
# Steps whose whole body runs under a lane slot (acquired in _run_step).
# GPU steps are NOT listed: they acquire the gpu lane per GPU call inside
# their method bodies (former heavy-slot sites).
STEP_LANES: Final[dict[str, str]] = {
    "download": "network",
    "extract_audio": "ffmpeg",
    "trim_initial_silence": "ffmpeg",
    "segment_audio": "ffmpeg",
}


def lane_for_step(name: str) -> str | None:
    return STEP_LANES.get(name)
```

- [ ] **Step 4: Run** `pytest tests/test_dag_tail.py -q` — PASS.
- [ ] **Step 5: Commit** `feat(pipeline): step-to-lane mapping (vts-rhs)`

---

### Task 4: `waiting` task status (model, migration, recovery, MCP literal)

**Model:** Sonnet 5 — mechanical enum/migration work with an exact precedent (0003).

**Files:**
- Modify: `vts/db/models.py:30-37` (TaskStatus)
- Create: `alembic/versions/0013_task_status_waiting.py`
- Modify: `vts/db/repo.py:168-177` (`requeue_running_tasks`)
- Modify: `vts/mcp/schemas.py:10-12` (TaskStatusLiteral)
- Test: `tests/test_task_transitions.py`, `tests/mcp/test_schemas.py`

**Interfaces:**
- Produces: `TaskStatus.waiting` ("waiting"); recovery treats waiting like running. Tasks 5–9 rely on `TaskStatus.waiting` existing.

- [ ] **Step 1: Write failing tests.** In `tests/test_task_transitions.py` add:

```python
def test_waiting_status_exists():
    from vts.db.models import TaskStatus
    assert TaskStatus.waiting.value == "waiting"
```

plus (async, using this file's existing session/repo fixtures — follow the local pattern) a `requeue_running_tasks` test: seed one task with `status=TaskStatus.waiting`, one `running`, one `queued`; call `repo.requeue_running_tasks()`; assert both waiting and running became `queued` and the returned list has both ids. In `tests/mcp/test_schemas.py`, extend the literal check with `"waiting"`.

- [ ] **Step 2: Run** `pytest tests/test_task_transitions.py tests/mcp/test_schemas.py -q` — FAIL.
- [ ] **Step 3: Implement.**
  - `models.py`: add `waiting = "waiting"` after `running` in `TaskStatus`.
  - `repo.py` `requeue_running_tasks`: change the select to `Task.status.in_([TaskStatus.running, TaskStatus.waiting])`.
  - `mcp/schemas.py`: add `"waiting"` to `TaskStatusLiteral` (after `"running"`).
  - Migration: copy `alembic/versions/0003_task_status_archived.py` to `0013_task_status_waiting.py`; set `revision = "0013_task_status_waiting"`, `down_revision` = the `revision` string found inside `alembic/versions/0012_user_step_weights.py` (read it); OLD enum = current 7 values, NEW enum = 8 with `"waiting"` after `"running"`; keep the same `op.alter_column` shape.
  - Check `vts/mcp/tools.py` `_wait_condition_met` and the task-list/status helpers for exhaustive status enumerations (grep `TaskStatus.` in `vts/mcp/` and `vts/api/`); `waiting` must behave like `running` (active, non-terminal) anywhere statuses are enumerated. If nothing enumerates, no change.
- [ ] **Step 4: Run** `pytest -q` — PASS (full suite: the literal is used in mcp server tests).
- [ ] **Step 5: Commit** `feat(db): waiting task status + recovery + mcp literal (vts-rhs)`

---

### Task 5: Processor — GPU lane replaces heavy_slot, waiting/running transitions

**Model:** Opus 4.8 — touches 7 call sites across a 2400-line module, status-race handling.

**Files:**
- Modify: `vts/pipeline/processor.py` (init ~line 85; heavy-slot sites at ~766, 874, 891, 1085, 1380, 1668, 1926)
- Modify: `vts/db/repo.py` (new `transition_task_status`)
- Modify: `tests/test_pipeline_resume.py`, `tests/test_finalize_loop.py`, `tests/test_segmentation_mode.py` (replace `_DummyHeavySlot` wiring)
- Test: `tests/test_processor_lanes.py` (new)

**Interfaces:**
- Consumes: `LaneManager.slot(...)` (Task 2), `TaskStatus.waiting` (Task 4).
- Produces:
  - `TaskProcessor.__init__(..., lanes: LaneManager | None = None)`; `self.lanes = lanes or LaneManager(settings)`; `self.heavy_slot` attribute REMOVED.
  - `TaskProcessor._gpu_slot(task_id: uuid.UUID, user_id: str, cls: str)` → async CM wrapping `self.lanes.slot("gpu", task_id, cls, on_wait=..., on_grant=...)`.
  - `Repo.transition_task_status(task_id: uuid.UUID, from_statuses: list[TaskStatus], to_status: TaskStatus) -> bool` — conditional UPDATE, returns True if a row changed.

- [ ] **Step 1: Add `Repo.transition_task_status` with failing test** (in `tests/test_task_transitions.py`): transition from `[running]`→`waiting` succeeds on a running task and returns True; returns False (and leaves status) on a canceled task. Implementation in `vts/db/repo.py`:

```python
    async def transition_task_status(
        self, task_id: uuid.UUID, from_statuses: list[TaskStatus], to_status: TaskStatus
    ) -> bool:
        result = await self.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.in_(from_statuses))
            .values(status=to_status, updated_at=utcnow())
        )
        await self.session.flush()
        return bool(result.rowcount)
```

- [ ] **Step 2: Processor status helpers + `_gpu_slot`.** In `TaskProcessor` add:

```python
    async def _mark_waiting(self, task_id: uuid.UUID, user_id: str, queue: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            changed = await repo.transition_task_status(
                task_id, [TaskStatus.running], TaskStatus.waiting
            )
            await session.commit()
        if changed:
            await self.bus.publish_event(
                user_id=user_id, task_id=str(task_id),
                event="task_status", data={"status": TaskStatus.waiting.value, "queue": queue},
            )

    async def _mark_running(self, task_id: uuid.UUID, user_id: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            changed = await repo.transition_task_status(
                task_id, [TaskStatus.waiting], TaskStatus.running
            )
            await session.commit()
        if changed:
            await self.bus.publish_event(
                user_id=user_id, task_id=str(task_id),
                event="task_status", data={"status": TaskStatus.running.value},
            )

    def _gpu_slot(self, task_id: uuid.UUID, user_id: str, cls: str):
        return self.lanes.slot(
            "gpu", task_id, cls,
            on_wait=lambda: self._mark_waiting(task_id, user_id, "gpu"),
            on_grant=lambda: self._mark_running(task_id, user_id),
        )
```

The conditional transition (`from [running]` only) is the race guard: a task the API just canceled/paused never gets overwritten to waiting.

- [ ] **Step 3: Replace the 7 heavy-slot sites.** In `__init__`: delete `self.heavy_slot = HeavySlot(redis, settings)`, delete the `HeavySlot` import, add `lanes` param as in Interfaces. Then per site, `async with self.heavy_slot:` becomes `async with self._gpu_slot(task_id, user_id, cls):` with cls per site — ~766 detect_language: `"asr"`; ~874 and ~891 transcribe segment (+retry): `"asr"`; ~1085 llama warmup: `"llm"`; ~1380 summarize window: `"llm"`; ~1668 pack batch: `"llm"`; ~1926 final summary: `"llm"`. Each site is inside a step method that already has `task_id` and `user_id` parameters — check each signature; where a helper lacks `user_id`, thread it through from the calling step method (grep call chain, keep signatures consistent). Update adjacent log strings `"waiting for heavy slot"`/`"heavy slot acquired"` to `"waiting for gpu slot"`/`"gpu slot acquired"`.
- [ ] **Step 3b: Shared-state audit.** `TaskProcessor` is one instance shared by all concurrent task coroutines. Review every `self.*` assignment in the class (grep `self\.[a-z_]+ =` inside methods, not just `__init__`): anything mutated per-task must be keyed by task_id (existing `_task_metrics`, `_task_n_ctx` already are). Also scan step methods for cross-task temp-file paths outside `dirs` (task artifact dirs are per-task and safe). Fix or report anything found.

- [ ] **Step 4: Fix existing tests.** In `tests/test_pipeline_resume.py`, `tests/test_finalize_loop.py`, `tests/test_segmentation_mode.py`: delete `_DummyHeavySlot` and every `processor.heavy_slot = _DummyHeavySlot()` line; instead add once per file:

```python
class _DummyLanes:
    def slot(self, lane, task_id, cls="main", *, on_wait=None, on_grant=None):
        class _CM:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
        return _CM()
```

and set `processor.lanes = _DummyLanes()` at each former heavy_slot line. Note the step methods now also need `user_id` where they didn't receive one before — pass the string the test already uses (grep each test's step invocation).

- [ ] **Step 5: New test** `tests/test_processor_lanes.py`: unit-test `_gpu_slot` transitions with a real `LaneManager` (1 gpu slot) and a stub `bus`/`session_factory`: hold the slot with one coroutine, enter `_gpu_slot` from another, assert `_mark_waiting` path ran (bus captured a `waiting` event with `queue == "gpu"`), then release and assert a `running` event followed. Stub `session_factory` via an object whose `Repo` interactions are monkeypatched — monkeypatch `TaskProcessor._mark_waiting`/`_mark_running` capture-style if full DB wiring is disproportionate; the DB-level transition guard is already covered in Step 1.
- [ ] **Step 6: Run** `pytest -q` — PASS.
- [ ] **Step 7: Commit** `feat(pipeline): gpu lane with asr priority replaces heavy slot (vts-rhs)`

---

### Task 6: Processor — network/ffmpeg lanes in `_run_step`

**Model:** Opus 4.8 — control-flow change in the pipeline core, ordering of step-status vs lane acquisition.

**Files:**
- Modify: `vts/pipeline/processor.py` (`_run_step`, ~line 362)
- Test: `tests/test_processor_lanes.py` (extend)

**Interfaces:**
- Consumes: `lane_for_step` (Task 3), `_mark_waiting`/`_mark_running` (Task 5).

- [ ] **Step 1: Failing test** (extend `tests/test_processor_lanes.py`): with `LaneManager` configured `lane_network_slots=1`, run two concurrent fake `download`-lane bodies through the new `_run_step` lane wrapper path; assert the second one produced a `waiting` transition with `queue == "network"` and they never overlapped (record enter/exit timestamps around a 20 ms sleep body). Drive `_run_step` the same way existing processor tests drive step methods (TaskProcessor.__new__ + stubbed repo/session/bus) — or, if `_run_step`'s DB surface makes that disproportionate, extract the wrapper into a testable `_step_lane(task_id, user_id, step_name)` helper returning the CM and test that.
- [ ] **Step 2: Implement.** In `_run_step`, wrap the execution segment (from `await repo.set_step_status(step, StepStatus.running)` through `await method(...)`) so lane acquisition happens FIRST:

```python
        lane = lane_for_step(step_name)
        if lane is not None:
            lane_cm = self.lanes.slot(
                lane, task_id,
                on_wait=lambda: self._mark_waiting(task_id, user_id, lane),
                on_grant=lambda: self._mark_running(task_id, user_id),
            )
        else:
            lane_cm = contextlib.nullcontext()
        async with lane_cm:
            await repo.set_step_status(step, StepStatus.running)
            ... existing running-event publish, method call, completed/failed handling ...
```

Import `lane_for_step` from `vts.pipeline.types`, `contextlib` if not present. Step status stays `pending` while queued in the lane (the task-level `waiting` covers UI); step turns `running` only once the slot is granted.

- [ ] **Step 3: Run** `pytest -q` — PASS.
- [ ] **Step 4: Commit** `feat(pipeline): network/ffmpeg lanes around step execution (vts-rhs)`

---

### Task 7: Worker pool + snapshot publishing + per-task cancel

**Model:** Opus 4.8 — rewrite of the worker loop with lifecycle/cancellation edge cases.

**Files:**
- Modify: `vts/worker/main.py` (worker_loop, ~lines 46-147)
- Test: `tests/test_worker_pool.py` (new)

**Interfaces:**
- Consumes: `LaneManager` (Task 2), `TaskProcessor(lanes=...)` (Task 5).
- Produces: `WorkerPool` class in `vts/worker/main.py`:

```python
class WorkerPool:
    def __init__(self, *, session_factory, bus: RedisBus, processor: TaskProcessor, max_active: int) -> None: ...
    async def admit(self) -> bool          # dequeue up to capacity; True if anything admitted
    async def watch_cancels(self) -> None  # cancel active asyncio tasks whose task_id has a cancel request
    async def reap(self) -> None           # collect finished coroutines, log, clear cancel flags
    @property
    def active_count(self) -> int
```

- [ ] **Step 1: Failing tests** `tests/test_worker_pool.py` (Postgres engine via `tests/_db.py` like conftest; fake bus object with an in-memory cancel set; fake processor whose `process_task` waits on per-task `asyncio.Event`):
  - `admit` claims queued tasks up to `max_active`, sets them running (via existing `dequeue_task`), spawns coroutines; with 3 queued and max 2 → `active_count == 2`, third stays `queued`.
  - two admitted fake tasks run **concurrently** (both entered before either released).
  - `watch_cancels` cancels exactly the requested task; `reap` clears it and `active_count` drops; the other keeps running.
  - pre-start cancel path: cancel requested before admit → task skipped and marked canceled (mirror of current lines 93-100).
- [ ] **Step 2: Implement.** Move the current single-task loop body into `WorkerPool` (dict `self._active: dict[uuid.UUID, asyncio.Task]`, set `self._cancel_sent: set[uuid.UUID]`). `worker_loop` becomes:

```python
    lanes = LaneManager(
        settings,
        on_change=lambda snap: _publish_lane_snapshot(redis, settings.redis_prefix, snap),
    )
    processor = TaskProcessor(session_factory=SessionLocal, redis=redis, settings=settings, lanes=lanes)
    pool = WorkerPool(session_factory=SessionLocal, bus=bus, processor=processor,
                      max_active=settings.worker_max_active_tasks)
    ...
    while True:
        admitted = await pool.admit()
        await pool.watch_cancels()
        await pool.reap()
        if not admitted and pool.active_count == 0:
            wakeup.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=5.0)
        else:
            await asyncio.sleep(0.2)
```

with

```python
async def _publish_lane_snapshot(redis: Redis, prefix: str, snapshot: dict[str, list[str]]) -> None:
    await redis.setex(f"{prefix}queue:lanes", 10, json.dumps(snapshot))
```

Keep: startup `recover_pending_tasks`, pubsub pump, weights loop, finally-block teardown (now cancelling ALL active tasks). Delete: the `heavy_slots` reset (lines 52, 56-57). `admit` must keep the skip-canceled-before-start branch and `clear_cancel_request` on start; `reap` keeps the CancelledError/Exception logging and `clear_cancel_request`, and must also delete the task's entries from `pool._cancel_sent`.

- [ ] **Step 3: Run** `pytest tests/test_worker_pool.py -q` then `pytest -q` — PASS.
- [ ] **Step 4: Commit** `feat(worker): concurrent task pool, lane snapshot publishing (vts-rhs)`

---

### Task 8: API — lane positions and `queue` field

**Model:** Sonnet 5 — additive serialization changes with exact anchors.

**Files:**
- Modify: `vts/api/main.py` (`_get_cached_queue_positions` area ~line 381; `serialize_task` ~627; `serialize_task_compact` ~682; all serialize call sites — grep `serialize_task(`)
- Modify: `vts/api/schemas.py:147` (TaskOut), TaskCompactOut (~168)
- Test: `tests/test_api_task_progress.py`

**Interfaces:**
- Produces: `TaskOut.queue: str | None`, `TaskCompactOut.queue: str | None`; `serialize_task(..., lane_positions: dict[uuid.UUID, tuple[str, int]] | None = None)` (same for compact); helper `_get_lane_positions(redis, prefix) -> dict[uuid.UUID, tuple[str, int]]`.

- [ ] **Step 1: Failing tests** (in `tests/test_api_task_progress.py`, follow the existing SimpleNamespace pattern):

```python
def test_serialize_waiting_task_carries_lane_queue(tmp_path):
    task = _task(tmp_path, steps=[])
    task.status = TaskStatus.waiting
    payload = serialize_task(task, lane_positions={task.id: ("gpu", 2)})
    assert payload.queue == "gpu"
    assert payload.queue_position == 2

def test_serialize_queued_task_keeps_global_position(tmp_path):
    task = _task(tmp_path, steps=[])
    task.status = TaskStatus.queued
    payload = serialize_task(task, queue_positions={task.id: 3})
    assert payload.queue is None
    assert payload.queue_position == 3
```

plus a direct test for `_get_lane_positions` parsing `{"network": [a], "ffmpeg": [], "gpu_asr": [b, b], "gpu_llm": [c]}` → `{a: ("network",1), b: ("gpu",1), c: ("gpu",1)}` (dedupe first occurrence; llm positions count within their own class) using a fake redis object with an async `get`.

- [ ] **Step 2: Implement.**
  - `schemas.py`: add `queue: str | None = None` to `TaskOut` and `TaskCompactOut` (next to `queue_position`).
  - `main.py` beside `_get_cached_queue_positions`:

```python
async def _get_lane_positions(redis: Redis, prefix: str) -> dict[uuid.UUID, tuple[str, int]]:
    raw = await redis.get(f"{prefix}queue:lanes")
    if not raw:
        return {}
    data = json.loads(raw)
    out: dict[uuid.UUID, tuple[str, int]] = {}
    for public, key in (("network", "network"), ("ffmpeg", "ffmpeg"),
                        ("gpu", "gpu_asr"), ("gpu", "gpu_llm")):
        position = 0
        for raw_id in data.get(key, []):
            tid = uuid.UUID(raw_id)
            if tid in out:
                continue
            position += 1
            out[tid] = (public, position)
    return out
```

  - `serialize_task` / `serialize_task_compact`: add `lane_positions=None` param; when `task.status == TaskStatus.waiting` and `task.id in lane_positions`, set `queue, queue_position = lane_positions[task.id]`; else `queue=None` and keep current queued-position logic. Pass the new field into the `TaskOut(...)` / `TaskCompactOut(...)` constructors.
  - Every call site that passes `queue_positions` also fetches and passes `lane_positions=await _get_lane_positions(redis, settings.redis_prefix)` — grep `serialize_task(` and `_get_cached_queue_positions(` in `vts/api/main.py` and `vts/mcp/` (if mcp serializes, mirror there).
- [ ] **Step 3: Run** `pytest -q` — PASS.
- [ ] **Step 4: Commit** `feat(api): per-lane queue field and positions (vts-rhs)`

---

### Task 9: UI — waiting badge with lane and position

**Model:** Sonnet 5 — anchored frontend edits; remember app.js has no defer (new DOM ids must precede the script tag — not needed here, all changes are dynamic).

**Files:**
- Modify: `vts/static/app.js` (`setTaskStatusAppearance` ~1387; runtime build ~1169; queue-pos refresh ~2862; progress text ~1325/1355)
- Modify: `vts/static/i18n/ru.js`, `en.js`, `de.js` (status keys block, ru.js ~143-150)
- Modify: `vts/static/styles.css` (status badge classes — grep `status-queued`)
- Test: manual via `/verifier-web` in the final task; add key-parity coverage to `tests/test_i18n_tooltip_keys.py` ONLY if that test asserts full key parity across locales (read it first; if it is tooltip-specific, skip).

**Interfaces:**
- Consumes: `task.queue` + `task.queue_position` from Task 8; SSE `task_status` events now carry `{"status": "waiting", "queue": "gpu"}`.

- [ ] **Step 1: i18n keys** — add to all three locale files next to the existing `status.*` block:

| key | ru | en | de |
|---|---|---|---|
| `status.waiting` | `ждёт очереди` | `waiting` | `wartet` |
| `status.waiting_pos` | `ждёт: {queue} (№{position})` | `waiting: {queue} (#{position})` | `wartet: {queue} (Nr. {position})` |
| `queue.network` | `скачивание` | `download` | `Download` |
| `queue.ffmpeg` | `конвертация` | `conversion` | `Konvertierung` |
| `queue.gpu` | `GPU` | `GPU` | `GPU` |

- [ ] **Step 2: app.js.**
  - Runtime build (~1169): alongside `queuePosition: parseQueuePosition(task.queue_position),` add `queue: task.queue || null,`.
  - Queue-pos refresh (~2862): where `runtime.queuePosition` is updated from a positions payload, also update `runtime.queue` from the task's `queue` field (read the surrounding code; if that refresh path only carries positions for queued tasks, take `queue` from the task list payload instead).
  - `setTaskStatusAppearance(statusEl, status, queuePosition = null, queue = null)` (~1387): add before the `queued` branch:

```javascript
  if (status === "waiting") {
    statusEl.textContent = queue && queuePosition
      ? t("status.waiting_pos", { queue: t(`queue.${queue}`), position: queuePosition })
      : t("status.waiting");
    statusEl.className = `task-status status-waiting`;
    return;
  }
```

(match the exact className scheme used by the existing branches — read the function body first and mirror it). Update the caller (~1498) to pass `runtime.queue`.
  - Progress text helpers (~1325/1355): extend the `if (runtime.queuePosition)` early-returns to also fire for waiting (`if (runtime.queuePosition && (runtime.baseStatus === "queued" || runtime.baseStatus === "waiting"))` — read the local conditions and keep their shape).
  - SSE `task_status` handler: where `data.status` updates runtime status, store `data.queue` into `runtime.queue` when present.
- [ ] **Step 3: styles.css** — add `.status-waiting` duplicating the `.status-queued` color rules (grep for `status-queued` and mirror every rule, including dark theme if present).
- [ ] **Step 4:** `pytest -q` (i18n parity test if applicable), quick sanity: `node --check vts/static/app.js`.
- [ ] **Step 5: Commit** `feat(ui): waiting status badge with lane queue position (vts-rhs)`

---

### Task 10: Remove HeavySlot, settings cleanup, docs

**Model:** Sonnet 5 — deletions and doc edits with exact anchors.

**Files:**
- Delete: `vts/services/heavy_slot.py`
- Modify: `vts/core/config.py` (remove `heavy_slot_limit`), `config.yaml` (remove `heavy_slot:` block), `tests/test_config_yaml.py` (remove heavy_slot assertions)
- Modify: `docs/ARCHITECTURE.md` (~line 108 "Single heavy slot" paragraph; config table ~300), `docs/SPEC_COMPLIANCE.md` (lines ~35, ~49)

**Steps:**
- [ ] **Step 1:** `grep -rn "heavy_slot\|HeavySlot" vts/ tests/ docs/ config.yaml` — must show only the files above; fix any stragglers first.
- [ ] **Step 2:** Delete the file, the setting, the yaml block, the test assertions. Rewrite ARCHITECTURE.md §"Single heavy slot" as §"Resource lanes": describe LaneManager (lanes/slots, gpu asr>llm priority with `gpu_asr_burst`, night mode on gpu grants, per-GPU-call granularity), the worker pool (`worker_max_active_tasks`), and the `waiting` status. Config table: remove `heavy_slot.limit` row, add the five new rows with env names and defaults. SPEC_COMPLIANCE: point the two rows at `vts/worker/lanes.py`.
- [ ] **Step 3:** `pytest -q` — PASS.
- [ ] **Step 4: Commit** `chore: remove HeavySlot in favor of lanes; docs (vts-rhs)`

---

### Task 11: Final gates and handoff

**Model:** Sonnet 5 — checklist execution.

**Steps:**
- [ ] **Step 1:** Bump version in `vts/__init__.py` (minor bump — new feature).
- [ ] **Step 2:** Full `pytest -q`; `node --check vts/static/app.js`.
- [ ] **Step 3:** Invoke the `verifier-web` skill: verify a task card in `waiting` shows «ждёт: GPU (№1)» (stub `/api/tasks` with `status="waiting"`, `queue="gpu"`, `queue_position=1`) and that `queued` cards still render «очередь #N».
- [ ] **Step 4:** Invoke the `verify` skill (end-to-end sanity per repo policy).
- [ ] **Step 5:** Deployment note in commit body: hosts must drop `VTS_HEAVY_SLOT_LIMIT` from `/opt/vts/config/vts.env` if set; kill switch is `VTS_WORKER_MAX_ACTIVE_TASKS=1`. Alembic migration `0013` runs via the existing deploy path.
- [ ] **Step 6:** Commit `feat: parallel task processing via resource lanes (vts-rhs, VOS-85)` + version bump; `git pull --rebase && bd dolt push && git push`.
- [ ] **Step 7:** bd: close vts-rhs only after Victor confirms; sync Linear VOS-85 per bd↔Linear rules (In Progress → Done on close). Build tag `build-X.Y.Z` ONLY on explicit request.
