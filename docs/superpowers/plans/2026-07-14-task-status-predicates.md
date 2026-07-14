# Task-Status Semantic Predicates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ~50 raw `baseStatus === "..."` comparisons in the frontend and the scattered status allow-lists in the backend with named semantic predicates sourced once in Python and delivered to JS — with ZERO behavior change.

**Architecture:** A new pure Python module `vts/services/task_status.py` defines status-set constants and predicates over `TaskStatus`. Backend call sites switch to them. Pure-status flags (is_active/can_pause/…) ship to JS as a single `status_flags` map via a config endpoint; task-dependent restart capabilities ship per-task in `TaskOut.capabilities`. A new `vts/static/status-predicates.js` module exposes the predicates to `app.js`, which replaces its raw comparisons.

**Tech Stack:** Python 3.12 (StrEnum, pydantic), FastAPI, SQLAlchemy async, vanilla JS frontend, pytest + pytest-asyncio, verifier-web (Playwright).

**Spec:** `docs/superpowers/specs/2026-07-14-task-status-predicates-design.md`

**Session note:** This plan was authored in a prior session and is intended to be executed in a SEPARATE session. Starting point: `main` at or after commit that adds this plan; bd issue `vts-c2n` is already claimed. Create a working branch before Task 1. Full-suite baseline at authoring time: 635 passed.

## Global Constraints

- **Behavior-preserving.** Every predicate encodes EXACTLY the current status set (see the spec's "Обнаруженные факты"). No unification of the three different "terminal" sets, no changed behavior. Verification for every task: `pytest -q` stays green AND the diff only renames/routes logic. Full-suite baseline: 635 passed.
- **Do NOT fix the known divergences** (waiting non-archivable; MCP-terminal lacking `archived`). They are out of scope — separate bd issues. If you notice more while naming sets, note them, don't fix them.
- **Python is the single source.** JS never re-implements a predicate's rule — it reads the delivered `status_flags` map (pure-status) or `runtime.capabilities` (task-dependent).
- **Predicate/set names are exact** (used across tasks): sets `ACTIVE_STATUSES`, `PENDING_STATUSES`, `FINISHED_STATUSES`, `PAUSABLE_STATUSES`, `RESUMABLE_STATUSES`, `ARCHIVABLE_STATUSES`, `SKIPPABLE_ON_START_STATUSES`, `TERMINAL_FOR_WAIT_STATUSES`; functions `is_active`, `is_pending`, `is_finished`, `shows_progress`, `can_pause`, `can_resume`, `can_archive`, `is_skippable_on_start`, `is_terminal_for_wait`, `status_flags`.
- **app.js has no `defer`:** `status-predicates.js` MUST be included BEFORE `<script src="app.js">` in index.html (project memory: script-dom-order).
- **Version bump** in `vts/__init__.py` happens ONCE, in the final task. Docs/spec commits never bump.
- Python is NOT on PATH — always use `/home/victor/dev/vts/.venv/bin/python -m pytest ...`.
- Commit after every task.

## File Structure

```
vts/services/task_status.py        # NEW: status-set constants + predicates + status_flags()
vts/api/main.py                    # switch can_pause_task/can_resume_task/archive gate; add capabilities; add config endpoint
vts/api/schemas.py                 # TaskCapabilities model; capabilities field on TaskOut/TaskCompactOut
vts/pipeline/processor.py          # line 76 skip set -> is_skippable_on_start
vts/mcp/tools.py                   # _TERMINAL -> TERMINAL_FOR_WAIT_STATUSES / is_terminal_for_wait
vts/db/repo.py                     # line 186 requeue set -> ACTIVE_STATUSES
vts/static/status-predicates.js    # NEW: JS predicate module reading the delivered map + runtime.capabilities
vts/static/index.html              # include status-predicates.js before app.js
vts/static/app.js                  # replace ~50 raw comparisons with predicates
tests/test_task_status.py          # NEW: truth-table unit tests
tests/ui/scenarios/status-predicates.mjs  # NEW: verifier-web scenario
```

---

### Task 1: Python predicate module + truth-table tests

**Model:** Sonnet 5 — exact code given, deterministic, test-verified.

**Files:**
- Create: `vts/services/task_status.py`
- Test: `tests/test_task_status.py`

**Interfaces:**
- Produces (consumed by Tasks 2-3): the sets and functions named in Global Constraints. Signatures: pure-status functions take `TaskStatus` and return `bool`; `status_flags() -> dict[str, dict[str, bool]]` keyed by `TaskStatus.value`.

- [ ] **Step 1: Write the failing test** — `tests/test_task_status.py`. Encode each set as an explicit truth table so any future change is caught:

```python
import pytest
from vts.db.models import TaskStatus
from vts.services import task_status as ts

ALL = list(TaskStatus)

@pytest.mark.parametrize("status,expected", [
    (TaskStatus.queued, False), (TaskStatus.running, True), (TaskStatus.waiting, True),
    (TaskStatus.paused, False), (TaskStatus.completed, False), (TaskStatus.archived, False),
    (TaskStatus.failed, False), (TaskStatus.canceled, False),
])
def test_is_active(status, expected):
    assert ts.is_active(status) is expected

@pytest.mark.parametrize("status,expected", [
    (TaskStatus.queued, True), (TaskStatus.running, False), (TaskStatus.waiting, True),
    (TaskStatus.paused, False), (TaskStatus.completed, False), (TaskStatus.archived, False),
    (TaskStatus.failed, False), (TaskStatus.canceled, False),
])
def test_is_pending(status, expected):
    assert ts.is_pending(status) is expected

def test_can_pause_matches_legacy_set():
    assert {s for s in ALL if ts.can_pause(s)} == {TaskStatus.queued, TaskStatus.running, TaskStatus.waiting}

def test_can_resume_matches_legacy_set():
    assert {s for s in ALL if ts.can_resume(s)} == {TaskStatus.paused, TaskStatus.failed}

def test_can_archive_matches_legacy_set():
    assert {s for s in ALL if ts.can_archive(s)} == {TaskStatus.completed, TaskStatus.failed}

def test_shows_progress_set():
    assert {s for s in ALL if ts.shows_progress(s)} == {
        TaskStatus.running, TaskStatus.waiting, TaskStatus.completed, TaskStatus.failed}

def test_is_finished_set():
    assert {s for s in ALL if ts.is_finished(s)} == {
        TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled, TaskStatus.archived}

def test_skippable_on_start_set():
    assert {s for s in ALL if ts.is_skippable_on_start(s)} == {
        TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}

def test_terminal_for_wait_set():
    assert {s for s in ALL if ts.is_terminal_for_wait(s)} == {
        TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled}

def test_status_flags_covers_all_statuses_and_matches_predicates():
    flags = ts.status_flags()
    assert set(flags) == {s.value for s in ALL}
    for s in ALL:
        f = flags[s.value]
        assert f == {
            "is_active": ts.is_active(s), "is_pending": ts.is_pending(s),
            "is_finished": ts.is_finished(s), "shows_progress": ts.shows_progress(s),
            "can_pause": ts.can_pause(s), "can_resume": ts.can_resume(s),
            "can_archive": ts.can_archive(s),
        }
```

- [ ] **Step 2: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_task_status.py -q` — expect FAIL (module missing).
- [ ] **Step 3: Implement** `vts/services/task_status.py`:

```python
"""Single source of task-status semantics. Pure functions over TaskStatus.

Each set encodes EXACTLY a status group used elsewhere in the codebase (see the
vts-c2n spec). Do NOT unify the three different "terminal" sets — they answer
different questions and any behavior change belongs in a separate issue.
"""
from __future__ import annotations

from vts.db.models import TaskStatus

ACTIVE_STATUSES = {TaskStatus.running, TaskStatus.waiting}
PENDING_STATUSES = {TaskStatus.queued, TaskStatus.waiting}
FINISHED_STATUSES = {
    TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled, TaskStatus.archived,
}
PAUSABLE_STATUSES = {TaskStatus.queued, TaskStatus.running, TaskStatus.waiting}
RESUMABLE_STATUSES = {TaskStatus.paused, TaskStatus.failed}
ARCHIVABLE_STATUSES = {TaskStatus.completed, TaskStatus.failed}
SKIPPABLE_ON_START_STATUSES = {TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}
TERMINAL_FOR_WAIT_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled}


def is_active(status: TaskStatus) -> bool:
    return status in ACTIVE_STATUSES


def is_pending(status: TaskStatus) -> bool:
    return status in PENDING_STATUSES


def is_finished(status: TaskStatus) -> bool:
    return status in FINISHED_STATUSES


def shows_progress(status: TaskStatus) -> bool:
    return is_active(status) or status in {TaskStatus.completed, TaskStatus.failed}


def can_pause(status: TaskStatus) -> bool:
    return status in PAUSABLE_STATUSES


def can_resume(status: TaskStatus) -> bool:
    return status in RESUMABLE_STATUSES


def can_archive(status: TaskStatus) -> bool:
    return status in ARCHIVABLE_STATUSES


def is_skippable_on_start(status: TaskStatus) -> bool:
    return status in SKIPPABLE_ON_START_STATUSES


def is_terminal_for_wait(status: TaskStatus) -> bool:
    return status in TERMINAL_FOR_WAIT_STATUSES


def status_flags() -> dict[str, dict[str, bool]]:
    """Pure-status flags for the frontend, delivered once at bootstrap."""
    return {
        s.value: {
            "is_active": is_active(s),
            "is_pending": is_pending(s),
            "is_finished": is_finished(s),
            "shows_progress": shows_progress(s),
            "can_pause": can_pause(s),
            "can_resume": can_resume(s),
            "can_archive": can_archive(s),
        }
        for s in TaskStatus
    }
```

- [ ] **Step 4: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_task_status.py -q && /home/victor/dev/vts/.venv/bin/python -m pytest -q` — both PASS (full suite still 635 + new tests).
- [ ] **Step 5: Commit** `feat(status): task_status predicate module (vts-c2n)`

---

### Task 2: Switch backend call sites to predicates

**Model:** Sonnet 5 — exact edit points named, behavior-preserving substitution.

**Files:**
- Modify: `vts/api/main.py:92-97` (can_pause_task/can_resume_task), `:1954` (archive gate)
- Modify: `vts/pipeline/processor.py:76` (skip set)
- Modify: `vts/mcp/tools.py:547,552,562` (_TERMINAL)
- Modify: `vts/db/repo.py:186` (requeue set)
- Test: existing suite (no new test; the predicates' truth tables + existing behavior tests cover it)

**Interfaces:**
- Consumes: `vts.services.task_status` (Task 1).

- [ ] **Step 1: main.py** — replace the two function bodies (keep the names, they are the public call surface used at :1862/:1890):
```python
from vts.services import task_status as _ts  # add near other imports

def can_pause_task(status: TaskStatus) -> bool:
    return _ts.can_pause(status)

def can_resume_task(status: TaskStatus) -> bool:
    return _ts.can_resume(status)
```
At the archive gate (~:1954), replace `if task.status not in {TaskStatus.completed, TaskStatus.failed}:` with `if not _ts.can_archive(task.status):`.
- [ ] **Step 2: processor.py:76** — replace `if task.status in {TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}:` with `if _ts.is_skippable_on_start(task.status):` (add `from vts.services import task_status as _ts`).
- [ ] **Step 3: mcp/tools.py** — replace `_TERMINAL = {"completed", "failed", "canceled"}` and its two uses. Since MCP compares `str(task.status)`, define locally `_TERMINAL = {s.value for s in task_status.TERMINAL_FOR_WAIT_STATUSES}` (import `from vts.services import task_status`), keeping the `.value` string comparison identical.
- [ ] **Step 4: repo.py:186** — replace `select(Task).where(Task.status.in_([TaskStatus.running, TaskStatus.waiting]))` with `.in_(list(task_status.ACTIVE_STATUSES))` (import the module). Add a one-line comment that this is the recovery/requeue "active" set.
- [ ] **Step 5: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS (existing can_pause/resume, transitions, MCP-wait tests unchanged and green).
- [ ] **Step 6: Commit** `refactor(status): route backend call sites through predicates (vts-c2n)`

---

### Task 3: API — capabilities in TaskOut + status_flags config endpoint

**Model:** Opus 4.8 — API surface + serializer wiring across two schemas + SSE-freshness reasoning.

**Files:**
- Modify: `vts/api/schemas.py` (TaskCapabilities model; `capabilities` on TaskOut ~:147 and TaskCompactOut ~:168)
- Modify: `vts/api/main.py` (serialize_task ~:666-692, serialize_task_compact ~:724-752 add capabilities; new config endpoint)
- Test: `tests/test_api_task_progress.py` (capabilities), a new small test for the config endpoint (in `tests/test_status_config.py`)

**Interfaces:**
- Consumes: `can_restart_summary_task(task)`, `can_restart_final_summary_task(task)` (existing, main.py:110/122), `task_status.status_flags()` (Task 1).
- Produces: `TaskOut.capabilities: TaskCapabilities`, `TaskCompactOut.capabilities: TaskCapabilities`; endpoint `GET /api/status-config` → `{"status_flags": {...}}`.

- [ ] **Step 1: schemas.py** — add the model and the field on both schemas (after the `queue` field):
```python
class TaskCapabilities(BaseModel):
    can_restart_summary: bool = False
    can_restart_final_summary: bool = False
```
On `TaskOut` and `TaskCompactOut`: `capabilities: TaskCapabilities = Field(default_factory=TaskCapabilities)`. (pause/resume/archive are NOT here — they are pure-status, delivered in the map.)
- [ ] **Step 2: Failing test** for capabilities (in `tests/test_api_task_progress.py`, SimpleNamespace pattern already used there): a completed summary task → `payload.capabilities.can_restart_summary is True`; a queued task → both False. Follow the file's `_task(...)` helper; set `status`, `options={"prompts":[{"source":"system","id":"summary"}]}`, and steps as needed.
- [ ] **Step 3: serialize_task / serialize_task_compact** — before constructing the return, compute:
```python
capabilities = {
    "can_restart_summary": can_restart_summary_task(task),
    "can_restart_final_summary": can_restart_final_summary_task(task),
}
```
and pass `capabilities=capabilities` into `TaskOut(...)` / `TaskCompactOut(...)`. Note `can_restart_*` read `task.steps`; the compact list path already loads steps (verify `get_tasks_for_user(..., load_steps=True)` at the compact call site — if steps aren't loaded there, guard `can_restart_*` against a missing-steps case exactly as they do today, no behavior change).
- [ ] **Step 4: config endpoint** — add near `/api/version` (~:1159):
```python
@app.get("/api/status-config")
async def status_config() -> JSONResponse:
    from vts.services.task_status import status_flags
    return JSONResponse({"status_flags": status_flags()}, headers=no_cache_headers)
```
- [ ] **Step 5: Failing test** `tests/test_status_config.py` — hit the endpoint via the existing `client` fixture; assert `data["status_flags"]` has all 8 statuses and, spot-check, `status_flags["waiting"]["is_active"] is True` and `["queued"]["shows_progress"] is False`.
- [ ] **Step 6: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_api_task_progress.py tests/test_status_config.py -q && /home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS.
- [ ] **Step 7: Commit** `feat(api): task capabilities + status_flags config endpoint (vts-c2n)`

---

### Task 4: JS predicate module + wiring

**Model:** Sonnet 5 — self-contained JS module + two include/bootstrap edits.

**Files:**
- Create: `vts/static/status-predicates.js`
- Modify: `vts/static/index.html` (include before app.js, ~:718)
- Modify: `vts/static/app.js` (load the map at bootstrap in `refreshAll`, ~:3045)
- Test: `node --check`; deferred behavioral check to Task 5's verifier-web scenario

**Interfaces:**
- Consumes: `GET /api/status-config` (Task 3), `runtime.capabilities` (from TaskOut, Task 3).
- Produces (used by app.js in Task 5): a global `window.statusPred` with:
  ```js
  statusPred.setFlags(map)        // store the delivered status_flags map
  statusPred.isActive(status)     // + isPending, isFinished, showsProgress, canPause, canResume, canArchive
  statusPred.canRestartSummary(runtime)        // reads runtime.capabilities
  statusPred.canRestartFinalSummary(runtime)
  ```

- [ ] **Step 1: Create `vts/static/status-predicates.js`:**
```javascript
// Single frontend source of task-status semantics. Pure-status flags come from
// the backend's /api/status-config map (vts.services.task_status.status_flags);
// task-dependent capabilities come from each task's runtime.capabilities.
// No status rule is re-implemented here (vts-c2n).
(function () {
  let FLAGS = {};
  function flag(status, key) {
    const row = FLAGS[String(status || "")];
    return Boolean(row && row[key]);
  }
  window.statusPred = {
    setFlags(map) { FLAGS = map && typeof map === "object" ? map : {}; },
    isActive: (s) => flag(s, "is_active"),
    isPending: (s) => flag(s, "is_pending"),
    isFinished: (s) => flag(s, "is_finished"),
    showsProgress: (s) => flag(s, "shows_progress"),
    canPause: (s) => flag(s, "can_pause"),
    canResume: (s) => flag(s, "can_resume"),
    canArchive: (s) => flag(s, "can_archive"),
    canRestartSummary: (rt) => Boolean(rt && rt.capabilities && rt.capabilities.can_restart_summary),
    canRestartFinalSummary: (rt) => Boolean(rt && rt.capabilities && rt.capabilities.can_restart_final_summary),
  };
})();
```
- [ ] **Step 2: index.html** — add BEFORE the app.js script (line ~718), so the global exists when app.js runs (app.js has no defer):
```html
    <script src="/static/status-predicates.js?v=__VTS_VERSION__"></script>
    <script src="/static/app.js?v=__VTS_VERSION__"></script>
```
- [ ] **Step 3: app.js bootstrap** — in `refreshAll()` (~:3045), before `loadTasks()`, fetch and store the map:
```javascript
  try {
    const cfg = await api("/api/status-config");
    if (cfg && cfg.status_flags) window.statusPred.setFlags(cfg.status_flags);
  } catch { /* predicates degrade to false; loadTasks still renders */ }
```
Also ensure `createRuntime(task)` carries `capabilities: task.capabilities || {}` onto the runtime (add that field next to `queue`).
- [ ] **Step 4: Run** `node --check vts/static/status-predicates.js && node --check vts/static/app.js` — both OK. Full `/home/victor/dev/vts/.venv/bin/python -m pytest -q` (unchanged; backend untouched here).
- [ ] **Step 5: Commit** `feat(ui): status-predicates.js module + bootstrap wiring (vts-c2n)`

---

### Task 5: Replace raw comparisons in app.js + verifier-web

**Model:** Opus 4.8 — ~50 call-site judgments (group vs specific-status), UI verification.

**Files:**
- Modify: `vts/static/app.js` (the comparison sites)
- Create: `tests/ui/scenarios/status-predicates.mjs`

**Interfaces:**
- Consumes: `window.statusPred` (Task 4), `runtime.capabilities` (Task 3/4).

- [ ] **Step 1: Replace semantic-GROUP comparisons** with predicates; LEAVE comparisons that are genuinely about ONE specific status (e.g. a `completed`-only render branch) as `=== "completed"`. Concretely (line numbers approximate — grep each):
  - Buttons block (~1521-1535): `canPause = statusPred.canPause(runtime.baseStatus)`, `canResume = statusPred.canResume(runtime.baseStatus)`, `canArchive = statusPred.canArchive(runtime.baseStatus)`; restart flags → `statusPred.canRestartSummary(runtime)` / `canRestartFinalSummary(runtime)` (these currently derive from `completed`/`failed` + step state — replacing with the capability keeps behavior since Task 3 computes them from the same functions).
  - Progress functions (~1319-1386): keep the explicit `queued` branch; the `waiting`/active handling stays as fixed in vts-qzl but the "is this task done" checks use `statusPred.isFinished`/`showsProgress` where the intent is the group. Do NOT alter the vts-qzl waiting logic's outcome.
  - Active-state checks (~1238, 1572, 1595, 2642, 2729, 2758): `runtime.baseStatus === "running"` that mean "actively processing" → `statusPred.isActive(runtime.baseStatus)` ONLY where waiting should also count as active; where the code specifically needs `running` (e.g. a running-only timer start) KEEP `=== "running"`. Judge each: if the branch would be correct for a waiting task too, use isActive; else keep the literal.
  - Terminal checks (~2632, 2649): `=== "failed"` / `=== "completed" || === "failed"` — if the branch means "finished, fetch final data", use `statusPred.isFinished`; if it's failure-specific (error message parsing) keep `=== "failed"`.
  For EACH replaced site add no comment noise; for each KEPT literal that a reviewer might flag, add a short `// specific status, not a group` comment.
- [ ] **Step 2: verifier-web scenario** `tests/ui/scenarios/status-predicates.mjs` — stub `/api/status-config` with the real flags and `/api/tasks` with tasks in several statuses; assert observable button enablement and progress text per status:
```javascript
import { startStubServer, launch, openPage } from "../harness.mjs";
export const name = "status-predicates";
const FLAGS = {
  queued:{is_active:false,is_pending:true,is_finished:false,shows_progress:false,can_pause:true,can_resume:false,can_archive:false},
  running:{is_active:true,is_pending:false,is_finished:false,shows_progress:true,can_pause:true,can_resume:false,can_archive:false},
  waiting:{is_active:true,is_pending:true,is_finished:false,shows_progress:true,can_pause:true,can_resume:false,can_archive:false},
  paused:{is_active:false,is_pending:false,is_finished:false,shows_progress:false,can_pause:false,can_resume:true,can_archive:false},
  completed:{is_active:false,is_pending:false,is_finished:true,shows_progress:true,can_pause:false,can_resume:false,can_archive:true},
  failed:{is_active:false,is_pending:false,is_finished:true,shows_progress:true,can_pause:false,can_resume:true,can_archive:true},
  archived:{is_active:false,is_pending:false,is_finished:true,shows_progress:false,can_pause:false,can_resume:false,can_archive:false},
  canceled:{is_active:false,is_pending:false,is_finished:true,shows_progress:false,can_pause:false,can_resume:false,can_archive:false},
};
function task(id, status, extra={}) {
  return { id, source_url:"http://x/"+id, source_title:status, status,
    queue:null, queue_position:null, transcript_path:null, summary_path:null,
    options:{transcript:true, prompts:[{source:"system",id:"summary"}]}, steps:[],
    capabilities:{can_restart_summary:false, can_restart_final_summary:false},
    created_at:"2026-07-14T10:00:00Z", updated_at:"2026-07-14T10:00:00Z",
    progress:{transcribe:{current:0,total:0}, summary:{current:0,total:0}}, stats:{}, ...extra };
}
export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [task("11111111-1111-1111-1111-111111111111","running"),
                   task("22222222-2222-2222-2222-222222222222","paused")],
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="11111111-1111-1111-1111-111111111111"]`, { timeout: 5000 });
    // running: pause enabled, resume disabled/absent
    const btn = async (id, sel) => page.evaluate(([i,s]) => {
      const el = document.querySelector(`[data-task-id="${i}"] ${s}`);
      return el ? { present: true, disabled: el.disabled === true, hidden: el.classList.contains("hidden") } : { present: false };
    }, [id, sel]);
    const runPause = await btn("11111111-1111-1111-1111-111111111111", ".pause-btn");
    const pausedResume = await btn("22222222-2222-2222-2222-222222222222", ".resume-btn");
    if (runPause.present && (runPause.disabled || runPause.hidden)) failures.push("running task: pause button not actionable");
    if (pausedResume.present && (pausedResume.disabled || pausedResume.hidden)) failures.push("paused task: resume button not actionable");
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally { await browser.close(); server.close(); }
  return failures;
}
```
(Real selectors confirmed: `.pause-btn`, `.resume-btn`, `.archive-btn` — app.js:1632-1639. The harness serves the real static files.)
- [ ] **Step 3: Run** `cd /home/victor/dev/vts/tests/ui && node run.mjs` — `UI VERIFY: PASSED` incl. `status-predicates`. Then `node --check vts/static/app.js` and full `/home/victor/dev/vts/.venv/bin/python -m pytest -q`.
- [ ] **Step 4: Commit** `refactor(ui): app.js status comparisons via predicates (vts-c2n)`

---

### Task 6: Final gates + version bump

**Model:** Sonnet 5 — checklist.

- [ ] **Step 1:** Bump `vts/__init__.py` (patch: internal refactor, no behavior change — increment from the version then current).
- [ ] **Step 2:** Full `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — all green; capture pass count.
- [ ] **Step 3:** `cd tests/ui && node run.mjs` — `UI VERIFY: PASSED`.
- [ ] **Step 4:** Grep guard: `grep -n 'baseStatus === "' vts/static/app.js` — every remaining hit must be a genuinely specific-status branch (carrying the `// specific status` comment) or the count is materially reduced from ~30. List any remaining and confirm each is intentional in the commit body.
- [ ] **Step 5: Commit** `refactor: unified task-status predicates (vts-c2n)` + version bump; then per session-close: `git pull --rebase && bd dolt pull && bd dolt push && git push`.
- [ ] **Step 6:** bd: close vts-c2n after Victor confirms; file follow-up bd issues for the known divergences noted out-of-scope (waiting non-archivable; MCP-terminal lacking archived); mirror the pattern to Cognee `development_knowledge` per Knowledge Capture. No build tag unless Victor asks.
