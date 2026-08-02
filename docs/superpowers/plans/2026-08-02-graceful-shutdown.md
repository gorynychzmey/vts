# Graceful Shutdown Implementation Plan (vts-9er)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both VTS containers stop promptly and cleanly on SIGTERM, instead of hanging until the stop timeout and dying by SIGKILL.

**Architecture:** Two independent fixes for two different root causes, plus a small frontend courtesy. The worker installs signal handlers and cancels its main coroutine so its existing `finally` teardown runs. The web API already handles SIGTERM correctly but blocks waiting on never-ending SSE streams, so the app signals shutdown through an `asyncio.Event` that the `/api/events` generator watches; before closing it emits a `server_shutdown` event so the browser reconnects immediately rather than after its 2-second error backoff.

**Tech Stack:** Python 3.14, asyncio, FastAPI/uvicorn, Starlette `StreamingResponse`, redis pubsub, vanilla JS `EventSource`.

## Global Constraints

- **Diagnosis is settled — do not re-litigate it.** Measured on the live host:
  - `sudo podman exec vts-webapi sh -c 'kill -TERM 1'` → uvicorn logs `Shutting down` then `Waiting for connections to close.` and hangs for the full `--timeout-graceful-shutdown 15`. It DOES handle the signal; SSE keeps it alive.
  - `sudo podman exec vts-worker sh -c 'kill -TERM 1'` → nothing at all: no log line, uptime unchanged. `vts/worker/main.py` installs no signal handler, and as PID 1 the kernel applies no default action.
- **Version bump:** bump `vts/__init__.py` before committing (project rule).
- **Tests:** run with `/home/victor/dev/vts/.venv/bin/python -m pytest`. NEVER `uv` — this project has no pyproject.toml. Postgres must be running for DB tests (`sudo podman start vts-test-pg`).
- **`app.js` has no `defer`:** any new DOM element referenced via `getElementById` must appear before the `<script>` tag. This plan adds no DOM, so it does not apply — but do not introduce any.
- **Production runs a pod** (`vts.service`). Do NOT restart, stop, or otherwise touch production while implementing. Verification happens in tests and, at the end, on an explicitly-requested deploy.
- **Do not change the stop timeouts** (`--timeout-graceful-shutdown`, `TimeoutStopSec`) as a substitute for fixing the hang. They are the safety net, not the fix.

---

## File Structure

- `vts/worker/main.py` — MODIFY. Install SIGTERM/SIGINT handlers in `main()`; cancel `worker_loop` so its existing `finally` teardown runs.
- `vts/api/main.py` — MODIFY. Add a shutdown `asyncio.Event` to `app.state` in `lifespan`; set it on the way out; have the `/api/events` generator race its pubsub read against that event, emit `server_shutdown`, and return.
- `vts/static/app.js` — MODIFY. Handle the `server_shutdown` event: close the stream and reconnect promptly instead of waiting for `onerror`'s 2s backoff.
- `tests/test_worker_shutdown.py` — NEW. Covers the worker's signal handling.
- `tests/test_events_shutdown.py` — NEW. Covers the SSE generator exiting on the shutdown event.

---

## Task 1: Worker exits on SIGTERM

`vts/worker/main.py` currently runs `asyncio.run(worker_loop())` with no signal handling. `worker_loop` already has a complete `finally` block (cancels the pool, the pump, the weights loop, the upload-GC loop and the delivery loop, then closes pubsub and redis) — so cancelling the coroutine is enough to get a clean teardown.

**Files:**
- Modify: `vts/worker/main.py`
- Test: `tests/test_worker_shutdown.py`
- Modify: `vts/__init__.py` (version bump)

**Interfaces:**
- Produces: `async def _run_worker() -> None` — awaits `worker_loop()` as a task, installs `SIGTERM`/`SIGINT` handlers that cancel it, and swallows the resulting `CancelledError` so the process exits 0.
- `main()` keeps its signature (`def main() -> None`) and still calls `configure_logging()`; only what it runs changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_shutdown.py
"""The worker must exit on SIGTERM instead of ignoring it (vts-9er).

Measured before this fix: `podman stop -t 30 vts-worker` waited the full 30s
and then SIGKILLed, because the process is PID 1 with no handler installed.
"""
from __future__ import annotations

import asyncio
import signal

import pytest

import vts.worker.main as worker_main


@pytest.mark.asyncio
async def test_run_worker_cancels_the_loop_on_sigterm(monkeypatch):
    """SIGTERM must cancel worker_loop and let _run_worker return normally."""
    started = asyncio.Event()
    cleaned_up = False

    async def fake_worker_loop() -> None:
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            # Stands in for the real loop's teardown block.
            cleaned_up = True

    monkeypatch.setattr(worker_main, "worker_loop", fake_worker_loop)

    runner = asyncio.create_task(worker_main._run_worker())
    await asyncio.wait_for(started.wait(), timeout=5)

    signal.raise_signal(signal.SIGTERM)

    # Must finish promptly — the point of the fix is not waiting for a timeout.
    await asyncio.wait_for(runner, timeout=5)
    assert cleaned_up, "worker_loop's finally block must run"


@pytest.mark.asyncio
async def test_run_worker_returns_normally_without_a_signal(monkeypatch):
    """A loop that ends on its own must not be reported as a failure."""
    async def fake_worker_loop() -> None:
        return

    monkeypatch.setattr(worker_main, "worker_loop", fake_worker_loop)
    await asyncio.wait_for(worker_main._run_worker(), timeout=5)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_worker_shutdown.py -q`
Expected: FAIL with `AttributeError: module 'vts.worker.main' has no attribute '_run_worker'`.

- [ ] **Step 3: Implement**

In `vts/worker/main.py`, add above `def main()`:

```python
async def _run_worker() -> None:
    """Run worker_loop until it finishes or a termination signal arrives.

    The worker is PID 1 in its container, and the kernel applies no default
    action to signals for PID 1 — without an explicit handler SIGTERM is simply
    dropped, which is why stopping the container used to wait out the full
    timeout and end in SIGKILL (vts-9er).

    Cancelling the task is all that is needed: worker_loop's own `finally`
    already cancels the pool and every background loop and closes redis.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker_loop())
    log = logging.getLogger("vts.worker")

    def _request_stop(signame: str) -> None:
        if not task.done():
            log.info("received %s, shutting down", signame)
            task.cancel()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame)
        try:
            loop.add_signal_handler(sig, _request_stop, signame)
        except NotImplementedError:
            # Not available on every platform; the container is Linux, so this
            # is only a guard for exotic dev environments.
            pass

    try:
        await task
    except asyncio.CancelledError:
        # Expected: this is our own cancellation, not a failure.
        log.info("worker stopped")
```

Change `main()` to use it:

```python
def main() -> None:
    configure_logging()
    asyncio.run(_run_worker())
```

Add `import signal` to the imports at the top of the file (alongside `import asyncio`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_worker_shutdown.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Confirm nothing else broke**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_worker_pool.py tests/test_worker_shutdown.py -q`
Expected: PASS.

- [ ] **Step 6: Bump version and commit**

```bash
sed -i 's/__version__ = "1.5.43"/__version__ = "1.5.44"/' vts/__init__.py
git add vts/worker/main.py tests/test_worker_shutdown.py vts/__init__.py
git commit -m "fix(worker): exit on SIGTERM instead of waiting out the stop timeout (vts-9er)"
```

---

## Task 2: SSE stops blocking the web API's shutdown

`/api/events` (`vts/api/main.py`, the `get_events` endpoint) yields from a `while True` loop driven by `pubsub.get_message(..., timeout=30.0)`. That generator only ends when the client disconnects, so uvicorn's graceful shutdown waits for it. The fix gives the app a shutdown `asyncio.Event` and makes the generator watch it.

**Files:**
- Modify: `vts/api/main.py`
- Test: `tests/test_events_shutdown.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `app.state.shutting_down: asyncio.Event`, created in `lifespan` before `yield` and `.set()` in the `finally` before redis is closed. The SSE generator emits a final `event: server_shutdown` frame and returns when it is set.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_events_shutdown.py
"""The SSE stream must end when the app shuts down (vts-9er).

Before this fix the generator looped forever on pubsub, so uvicorn's graceful
shutdown blocked until --timeout-graceful-shutdown expired and the container
was SIGKILLed.
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_event_stream_ends_when_shutdown_is_signalled(client, authed_app):
    app, _factory = authed_app

    # The endpoint reads this off app.state; the lifespan does not run under
    # ASGITransport, so create it here the same way lifespan would.
    if not hasattr(app.state, "shutting_down"):
        app.state.shutting_down = asyncio.Event()
    app.state.shutting_down.clear()

    frames: list[str] = []

    async def read_stream() -> None:
        async with client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                frames.append(line)

    reader = asyncio.create_task(read_stream())
    await asyncio.sleep(0.5)          # let the generator subscribe
    app.state.shutting_down.set()

    # Must finish quickly: the whole point is not waiting for a timeout.
    await asyncio.wait_for(reader, timeout=10)

    body = "\n".join(frames)
    assert "event: server_shutdown" in body, body
```

- [ ] **Step 2: Run it and watch it fail**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_events_shutdown.py -q`
Expected: FAIL — the read times out, because the generator ignores the event.

- [ ] **Step 3: Create the event in `lifespan`**

In `vts/api/main.py`, in the `lifespan` context manager, create the event before the redis client and set it on the way out:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Watched by long-lived streams (/api/events) so they can end
        # themselves. Without it uvicorn's graceful shutdown waits for SSE
        # clients that never disconnect (vts-9er).
        app.state.shutting_down = asyncio.Event()
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            app.state.shutting_down.set()
            await app.state.redis.aclose()
```

`import asyncio` is ALREADY at line 3 of `vts/api/main.py` (verified) — do not add a duplicate.

- [ ] **Step 4: Make the generator watch it**

Replace the body of `event_generator` in the `get_events` endpoint with:

```python
        async def event_generator() -> Any:
            yield f"event: server_version\ndata: {json.dumps({'version': __version__}, ensure_ascii=True)}\n\n"
            shutting_down: asyncio.Event | None = getattr(app.state, "shutting_down", None)
            pubsub = redis.pubsub()
            channel = f"{settings.redis_prefix}events"
            await pubsub.subscribe(channel)
            try:
                while True:
                    if shutting_down is not None and shutting_down.is_set():
                        # Tell the client why, so it reconnects at once instead
                        # of waiting out its own error backoff.
                        yield "event: server_shutdown\ndata: {}\n\n"
                        return
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message:
                        # A short poll keeps the shutdown check responsive; the
                        # ping only goes out on the original ~30s cadence so the
                        # client sees no change in traffic.
                        continue
                    data = json.loads(message["data"].decode("utf-8"))
                    if data.get("user_id") != user.id:
                        continue
                    yield f"event: {data.get('event', 'message')}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"
            finally:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
```

**Careful — the ping must not become 30× more frequent.** The original loop emitted `event: ping` whenever a 30-second read timed out. The snippet above drops that ping entirely, which would break any client relying on it. Restore it on the original cadence with a counter:

```python
            idle_polls = 0
            ...
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message:
                        idle_polls += 1
                        if idle_polls >= 30:
                            idle_polls = 0
                            yield "event: ping\ndata: {}\n\n"
                        continue
                    idle_polls = 0
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_events_shutdown.py -q`
Expected: PASS.

- [ ] **Step 6: Confirm the ping cadence is intact**

Run: `grep -n "idle_polls\|event: ping\|timeout=1.0" vts/api/main.py`
Expected: the counter resets on every real message, and `ping` is emitted only every 30th idle poll — i.e. still roughly every 30 seconds.

- [ ] **Step 7: Commit**

```bash
git add vts/api/main.py tests/test_events_shutdown.py
git commit -m "fix(api): end SSE streams on shutdown so uvicorn can exit (vts-9er)"
```

---

## Task 3: Frontend reconnects immediately on `server_shutdown`

`connectEvents` in `vts/static/app.js` already recovers from a dropped stream: `onerror` closes the source and reconnects after 2 seconds. With the server now announcing its shutdown, the client can skip that blind wait — and, since a shutdown usually means a deploy, re-check the version on the way back.

**Files:**
- Modify: `vts/static/app.js`

**Interfaces:**
- Consumes: the `server_shutdown` SSE event from Task 2.
- Produces: no new globals; adds one listener inside the existing `connectEvents`.

- [ ] **Step 1: Add the listener**

In `vts/static/app.js`, inside `connectEvents`, next to the other `addEventListener` calls (before the `onerror` assignment), add:

```javascript
  // The server is stopping (deploy or restart). It tells us so we can come
  // back deliberately instead of waiting out onerror's blind 2s backoff.
  // A shutdown almost always means a new version is landing, so reconnect a
  // little later than usual and let the server_version handler pick it up.
  state.eventSource.addEventListener("server_shutdown", () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setTimeout(() => {
      connectEvents();
      void loadFirstPage();
    }, 1000);
  });
```

- [ ] **Step 2: Check it cannot double-reconnect**

Closing the source inside the handler prevents `onerror` from firing for the same stream, so only one reconnect is scheduled. Verify by reading the `onerror` handler directly above: it guards on `state.eventSource` being non-null, and this handler nulls it.

Run: `grep -n -A6 "server_shutdown" vts/static/app.js` and `grep -n -A8 "onerror" vts/static/app.js`
Expected: both guard on `state.eventSource` before acting.

- [ ] **Step 3: Syntax check**

Run: `node --check vts/static/app.js && echo "app.js OK"`
Expected: `app.js OK`. (node v22 is available on this host — verified, so this check must actually run.)

- [ ] **Step 4: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(ui): reconnect promptly when the server announces shutdown (vts-9er)"
```

---

## Task 4: Prove it on the real containers

Unit tests cover the logic; this task proves the actual stop time changed. It runs against the pod, so it briefly restarts containers.

**Files:** none (host verification)

- [ ] **Step 1: Confirm nothing is in flight**

```bash
sudo podman exec vts-webapi python -c "
import asyncio
from sqlalchemy import select, func
from vts.db.session import SessionLocal
from vts.db.models import Task, TaskStatus
async def main():
    async with SessionLocal() as s:
        for st in (TaskStatus.running, TaskStatus.queued):
            print(st.value, await s.scalar(select(func.count()).select_from(Task).where(Task.status==st)))
asyncio.run(main())"
```
Expected: zeros. If not, wait — a restart requeues in-flight work.

- [ ] **Step 2: Measure the stop time, after the fix is deployed**

```bash
time sudo podman stop -t 30 vts-worker
time sudo podman stop -t 30 vts-webapi
```
Expected: **seconds, not the full 30**. Before the fix these took exactly 30s and 15s respectively.

- [ ] **Step 3: Bring the pod back and confirm health**

```bash
sudo systemctl restart vts.service
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8086/
sudo podman ps --format "{{.Names}}\t{{.Status}}" | grep -E "^vts-(webapi|worker)"
```
Expected: HTTP 302, both containers up.

- [ ] **Step 4: Check the logs for the new shutdown paths**

```bash
sudo podman logs vts-worker 2>&1 | grep -iE "received SIGTERM|worker stopped" | tail -2
```
Expected: the worker logged its signal handling on the previous stop.

---

## Self-Review

**Spec coverage:** there is no separate spec — the issue (vts-9er) plus the measurements recorded on it are the requirements. Both measured root causes have a task: the worker's missing handler (Task 1) and the SSE block (Task 2). Task 3 is the client-side half of the "warn the client" decision. Task 4 verifies the observable behaviour that motivated the issue.

**Placeholder scan:** none. Task 3 Step 3 tolerates a missing `node`, which is a real environment condition, not a vague instruction.

**Type consistency:** `_run_worker` is used in Task 1 only. `app.state.shutting_down` is created in Task 2 Step 3 and read in Step 4 under the same name. The SSE event name `server_shutdown` is identical in Task 2 (emitter) and Task 3 (listener).

**Deliberate omission:** neither `--timeout-graceful-shutdown` nor `TimeoutStopSec` is changed. They exist as the backstop for a hang; raising or lowering them would hide the problem rather than fix it. Likewise `terminationGracePeriodSeconds` stays unset in `deploy/vts.yaml` — worth revisiting only if a stop still misbehaves after these fixes.

**Known risk carried into Task 2:** shortening the pubsub read from 30s to 1s makes the loop wake 30× more often. The counter keeps the visible ping cadence unchanged, but this is the one change here with a steady-state cost; if it ever matters, the alternative is racing `get_message` against `shutting_down.wait()` with `asyncio.wait(..., return_when=FIRST_COMPLETED)` instead of polling.
