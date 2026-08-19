# LLM Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `chat_completion` read an SSE stream so that closing the connection stops generation on the backend, progress is visible while a window is written, and timeouts key off the gap between tokens instead of total elapsed time.

**Architecture:** Four settings-driven timeout values feed a pure helper that computes a per-request ceiling from `max_tokens`. `chat_completion` switches from `client.post()` to `client.stream()`, accumulates `delta.content` chunks, and enforces three limits (first-chunk budget, inter-chunk idle, overall ceiling). The payload-fallback queue is untouched: every fallback trigger fires before the first chunk arrives. The function still returns a complete `str`, so no call site changes.

**Tech Stack:** Python 3.14, httpx (async), pytest, pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-08-19-llm-streaming-design.md`

## Global Constraints

- Python 3.14; run tests with the worktree's own `.venv/bin/python -m pytest` (the project uses `requirements.txt`, not uv).
- Tests need a local Postgres: `sudo podman start vts-test-pg` before running the suite. Tests touched by this plan are pure-unit and do not need it, but the full-suite gate does.
- Comments and commit messages in English; chat and bd notes in Russian.
- Bump `__version__` in `vts/__init__.py` before committing (project rule).
- New settings are added to `Settings` in `vts/core/config.py` AND to the `services_aliases` map, or the YAML key is silently ignored.
- Defaults must preserve current behaviour for anyone who does not set them.

---

### Task 1: Timeout settings and the ceiling helper

**Files:**
- Modify: `vts/core/config.py:140-141` (add settings next to `llm_chat_timeout_seconds`), `vts/core/config.py:510-511` (add aliases)
- Modify: `vts/services/summarizer.py` (add helper near `_loading_wait_seconds`, around line 162)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings.llm_min_tokens_per_second: float = 3.0`
  - `Settings.llm_ceiling_slack_multiplier: float = 1.5`
  - `Settings.llm_ceiling_floor_seconds: int = 300`
  - `Settings.llm_ceiling_cap_seconds: int = 3600`
  - `Settings.llm_stream_idle_timeout_seconds: int = 120`
  - `Settings.llm_stream_first_chunk_timeout_seconds: int = 300`
  - `vts.services.summarizer.derive_stream_ceiling(max_tokens: int | None, *, min_tokens_per_second: float, slack: float, floor_seconds: int, cap_seconds: int) -> float`

- [ ] **Step 1: Write the failing test**

`tests/test_summarizer.py` currently imports only `asyncio`, `MagicMock`,
`patch`, `httpx` and `pytest`. Tasks 2-4 also need `json` and
`AsyncIterator`, so add them to the top of the file now:

```python
import json
from collections.abc import AsyncIterator
```

Then add the test:

```python
def test_derive_stream_ceiling_clamps_to_floor_and_cap() -> None:
    from vts.services.summarizer import derive_stream_ceiling

    kw = dict(
        min_tokens_per_second=3.0,
        slack=1.5,
        floor_seconds=300,
        cap_seconds=3600,
    )
    # Segment window: the middle of the range, neither bound binds.
    assert derive_stream_ceiling(1255, **kw) == pytest.approx(627.5)
    # Short user prompt: raised to the floor, so a cold start is not cut short.
    assert derive_stream_ceiling(300, **kw) == 300
    # Final summary: clamped down to the cap.
    assert derive_stream_ceiling(15000, **kw) == 3600
    # No budget given (max_tokens=None) -> the cap is the only sane answer.
    assert derive_stream_ceiling(None, **kw) == 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_derive_stream_ceiling_clamps_to_floor_and_cap -v`
Expected: FAIL with `ImportError: cannot import name 'derive_stream_ceiling'`

- [ ] **Step 3: Add a module logger**

`vts/services/summarizer.py` currently logs nothing at all — it has no
`logger` — yet Task 4 must log stream progress and timeouts. Its import block
is also missing three names the later tasks need (`time`, `AsyncIterator`,
`Callable`). Fix both now so no later step trips over a `NameError`.

The file's imports today are exactly:

```python
from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
```

Replace that block with:

```python
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)
```

(The `logger` convention follows `vts/services/push.py`.)

Verify it still imports:

```bash
./.venv/bin/python -c "import vts.services.summarizer; print('ok')"
```

- [ ] **Step 4: Write the helper**

In `vts/services/summarizer.py`, directly after `_loading_wait_seconds`:

```python
def derive_stream_ceiling(
    max_tokens: int | None,
    *,
    min_tokens_per_second: float,
    slack: float,
    floor_seconds: int,
    cap_seconds: int,
) -> float:
    """Overall wall-clock ceiling for one streamed completion.

    Scaled to the work requested rather than fixed: a segment window and a
    final summary differ by an order of magnitude in output size, and one
    constant cannot serve both without being far too loose for the small case.

    A missing budget means "we do not know how much this will produce", so the
    cap is the only defensible answer — never the floor, which would cut off
    legitimate long work.
    """
    if max_tokens is None or max_tokens <= 0:
        return float(cap_seconds)
    raw = max_tokens / max(min_tokens_per_second, 0.1) * slack
    return float(max(floor_seconds, min(raw, cap_seconds)))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_derive_stream_ceiling_clamps_to_floor_and_cap -v`
Expected: PASS

- [ ] **Step 6: Add the settings**

In `vts/core/config.py`, immediately after line 141 (`llm_final_timeout_seconds: int = 1800`):

```python
    # Streaming timeouts (vts-gouq). The segment stage generates for 10-17
    # minutes at 9-13 tokens/s, so a total-duration timeout cannot separate
    # "slow but working" from "wedged" — silence between chunks can.
    llm_stream_idle_timeout_seconds: int = 120
    # Separate and much larger: it covers model load, measured at 75 s cold,
    # with time-to-first-token of 14-16 s once warm.
    llm_stream_first_chunk_timeout_seconds: int = 300
    # Overall ceiling = clamp(max_tokens / min_tps * slack, floor, cap).
    llm_min_tokens_per_second: float = 3.0
    llm_ceiling_slack_multiplier: float = 1.5
    llm_ceiling_floor_seconds: int = 300
    llm_ceiling_cap_seconds: int = 3600
```

In the `services_aliases` dict, after line 511 (`"services_llm_final_timeout_seconds"`):

```python
        "services_llm_stream_idle_timeout_seconds": "llm_stream_idle_timeout_seconds",
        "services_llm_stream_first_chunk_timeout_seconds": "llm_stream_first_chunk_timeout_seconds",
        "services_llm_min_tokens_per_second": "llm_min_tokens_per_second",
        "services_llm_ceiling_slack_multiplier": "llm_ceiling_slack_multiplier",
        "services_llm_ceiling_floor_seconds": "llm_ceiling_floor_seconds",
        "services_llm_ceiling_cap_seconds": "llm_ceiling_cap_seconds",
```

- [ ] **Step 7: Write the failing test for YAML mapping**

Add to `tests/test_summarizer.py`:

```python
def test_stream_settings_map_from_nested_yaml() -> None:
    from vts.core.config import _normalize_yaml_overrides

    got = _normalize_yaml_overrides(
        {"services": {"llm": {"stream_idle_timeout_seconds": 90, "min_tokens_per_second": 5}}}
    )
    assert got["llm_stream_idle_timeout_seconds"] == 90
    assert got["llm_min_tokens_per_second"] == 5
```

- [ ] **Step 8: Run both tests**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -k "stream_ceiling or stream_settings" -v`
Expected: PASS (the alias edit from Step 6 already satisfies the second test)

- [ ] **Step 9: Commit**

```bash
git add vts/core/config.py vts/services/summarizer.py tests/test_summarizer.py
git commit -m "feat(llm): settings and ceiling helper for streamed completions

Ceiling scales with max_tokens instead of being a constant: a segment
window (1255 tokens) and a final summary (~15000) differ by an order of
magnitude, and both bounds bind in practice — the short case is raised to
the floor, the long one clamped to the cap.

Part of vts-gouq."
```

---

### Task 2: SSE chunk parsing

**Files:**
- Modify: `vts/services/summarizer.py` (add helper next to `derive_stream_ceiling`)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `vts.services.summarizer.parse_sse_content(line: str) -> str | None` — returns the text of a `data:` line carrying `choices[0].delta.content`, `None` for anything else (blank lines, `[DONE]`, comments, malformed JSON, chunks with no content).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
def test_parse_sse_content_extracts_only_content_deltas() -> None:
    from vts.services.summarizer import parse_sse_content

    assert parse_sse_content(
        'data: {"choices":[{"delta":{"content":"Hello"}}]}'
    ) == "Hello"
    # A role-only opening chunk carries no text.
    assert parse_sse_content('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
    # Terminator, keep-alive comment, blank line, and non-SSE noise.
    assert parse_sse_content("data: [DONE]") is None
    assert parse_sse_content(": keep-alive") is None
    assert parse_sse_content("") is None
    assert parse_sse_content("event: ping") is None
    # Malformed JSON must not raise — a truncated frame is not fatal.
    assert parse_sse_content('data: {"choices":[{"delta"') is None
    # Empty-string content is not progress and must not reset the idle timer.
    assert parse_sse_content('data: {"choices":[{"delta":{"content":""}}]}') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_parse_sse_content_extracts_only_content_deltas -v`
Expected: FAIL with `ImportError: cannot import name 'parse_sse_content'`

- [ ] **Step 3: Write the parser**

In `vts/services/summarizer.py`, after `derive_stream_ceiling`:

```python
def parse_sse_content(line: str) -> str | None:
    """Text carried by one SSE line, or None if it carries none.

    Returns None rather than raising on malformed JSON: a stream can legally
    interleave comments and keep-alives, and one unparsable frame should not
    fail a completion that is otherwise arriving normally.

    Empty-string content counts as no content, so it cannot reset the idle
    timer — otherwise a backend emitting empty deltas would look alive while
    producing nothing.
    """
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        content = parsed["choices"][0]["delta"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str) or not content:
        return None
    return content
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_parse_sse_content_extracts_only_content_deltas -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/services/summarizer.py tests/test_summarizer.py
git commit -m "feat(llm): SSE content-delta parser

Tolerant by design: comments, keep-alives, [DONE] and malformed frames
return None instead of raising, and empty-string content is treated as no
content so it cannot keep the idle timer alive.

Part of vts-gouq."
```

---

### Task 3: Stream reader with the three limits

**Files:**
- Modify: `vts/services/summarizer.py` (add reader after `parse_sse_content`)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: `parse_sse_content` (Task 2).
- Produces:
  - `vts.services.summarizer.StreamTimeout(RuntimeError)` — raised when a limit is hit; carries `.reason` (`"first_chunk"`, `"idle"`, `"ceiling"`), `.chunks`, `.elapsed`.
  - `async vts.services.summarizer.read_sse_stream(lines: AsyncIterator[str], *, first_chunk_timeout: float, idle_timeout: float, ceiling: float, clock: Callable[[], float], on_progress: Callable[[int, float], None] | None = None) -> str`

`clock` is injected so tests drive time deterministically instead of sleeping. `on_progress(chunks, elapsed)` is called every 100 chunks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_summarizer.py`:

```python
class _FakeClock:
    """Monotonic clock the test advances by hand, so no test ever sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _lines(*items: str) -> "AsyncIterator[str]":
    for item in items:
        yield item


def test_read_sse_stream_accumulates_content() -> None:
    from vts.services.summarizer import read_sse_stream

    clock = _FakeClock()
    out = asyncio.run(
        read_sse_stream(
            _lines(
                'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                "data: [DONE]",
            ),
            first_chunk_timeout=300,
            idle_timeout=120,
            ceiling=600,
            clock=clock,
        )
    )
    assert out == "Hello"


def test_read_sse_stream_fails_on_idle_gap() -> None:
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def gappy() -> "AsyncIterator[str]":
        yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
        clock.now += 200.0  # silence longer than idle_timeout
        yield 'data: {"choices":[{"delta":{"content":"b"}}]}'

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                gappy(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=6000,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "idle"
    assert excinfo.value.chunks == 1


def test_read_sse_stream_fails_when_first_chunk_never_arrives() -> None:
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def slow_start() -> "AsyncIterator[str]":
        yield ": keep-alive"
        clock.now += 400.0  # longer than first_chunk_timeout
        yield ": keep-alive"

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                slow_start(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=6000,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "first_chunk"
    assert excinfo.value.chunks == 0


def test_read_sse_stream_fails_when_ceiling_exceeded() -> None:
    from vts.services.summarizer import StreamTimeout, read_sse_stream

    clock = _FakeClock()

    async def steady() -> "AsyncIterator[str]":
        for _ in range(100):
            clock.now += 10.0  # each gap is under idle_timeout
            yield 'data: {"choices":[{"delta":{"content":"x"}}]}'

    with pytest.raises(StreamTimeout) as excinfo:
        asyncio.run(
            read_sse_stream(
                steady(),
                first_chunk_timeout=300,
                idle_timeout=120,
                ceiling=200,
                clock=clock,
            )
        )
    assert excinfo.value.reason == "ceiling"


def test_read_sse_stream_allows_slow_but_steady_generation() -> None:
    """The regression this whole feature exists for.

    Under the old total-duration timeout a 14-minute window was discarded
    seconds before it landed. Steady chunks must succeed no matter how long
    the whole thing takes, as long as each gap is short.
    """
    from vts.services.summarizer import read_sse_stream

    clock = _FakeClock()

    async def slow() -> "AsyncIterator[str]":
        for _ in range(60):
            clock.now += 30.0  # 30 min total, every gap well under idle_timeout
            yield 'data: {"choices":[{"delta":{"content":"y"}}]}'

    out = asyncio.run(
        read_sse_stream(
            slow(),
            first_chunk_timeout=300,
            idle_timeout=120,
            ceiling=100_000,
            clock=clock,
        )
    )
    assert out == "y" * 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -k read_sse_stream -v`
Expected: FAIL with `ImportError: cannot import name 'read_sse_stream'`

- [ ] **Step 3: Write the reader**

In `vts/services/summarizer.py`, after `parse_sse_content`:

```python
class StreamTimeout(RuntimeError):
    """A streamed completion hit one of its three limits.

    Carries what was seen so the caller can log it: the whole point of this
    feature is that a stalled request reports how far it got instead of going
    silent for 17 minutes.
    """

    def __init__(self, reason: str, *, chunks: int, elapsed: float) -> None:
        super().__init__(
            f"llm stream {reason} timeout after {elapsed:.1f}s and {chunks} chunks"
        )
        self.reason = reason
        self.chunks = chunks
        self.elapsed = elapsed


async def read_sse_stream(
    lines: AsyncIterator[str],
    *,
    first_chunk_timeout: float,
    idle_timeout: float,
    ceiling: float,
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[int, float], None] | None = None,
) -> str:
    """Accumulate an SSE completion, enforcing three independent limits.

    The limits answer different questions. `first_chunk_timeout` covers model
    load, which is slow and says nothing about health. `idle_timeout` is the
    real stall detector once text is flowing. `ceiling` is a backstop against
    generation that never ends.

    `clock` is injectable so tests drive time without sleeping.
    """
    started = clock()
    last_seen = started
    chunks = 0
    buffer: list[str] = []

    async for line in lines:
        now = clock()
        if now - started > ceiling:
            raise StreamTimeout("ceiling", chunks=chunks, elapsed=now - started)
        limit = idle_timeout if chunks else first_chunk_timeout
        if now - last_seen > limit:
            reason = "idle" if chunks else "first_chunk"
            raise StreamTimeout(reason, chunks=chunks, elapsed=now - started)
        content = parse_sse_content(line)
        if content is None:
            continue
        buffer.append(content)
        chunks += 1
        last_seen = now
        if on_progress is not None and chunks % 100 == 0:
            on_progress(chunks, now - started)

    now = clock()
    if not chunks and now - last_seen > first_chunk_timeout:
        raise StreamTimeout("first_chunk", chunks=0, elapsed=now - started)
    return "".join(buffer)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -k read_sse_stream -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add vts/services/summarizer.py tests/test_summarizer.py
git commit -m "feat(llm): SSE reader enforcing first-chunk, idle and ceiling limits

Three limits answering three different questions: model load is slow but
healthy, silence mid-stream is a stall, and an unbounded run needs a
backstop. The clock is injected so the tests never sleep.

Includes the regression this feature exists for: a 30-minute stream with
short gaps must succeed, where the old total-duration timeout killed it.

Part of vts-gouq."
```

---

### Task 4: Switch `chat_completion` to streaming

**Files:**
- Modify: `vts/services/summarizer.py:596-700` (`chat_completion`)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: `derive_stream_ceiling` (Task 1), `read_sse_stream`, `StreamTimeout` (Task 3).
- Produces: `chat_completion` keeps its signature and `-> str` return. New keyword-only params, all defaulted so existing callers are unaffected: `stream_idle_timeout: float = 120.0`, `stream_first_chunk_timeout: float = 300.0`, `min_tokens_per_second: float = 3.0`, `ceiling_slack: float = 1.5`, `ceiling_floor_seconds: int = 300`, `ceiling_cap_seconds: int = 3600`.

**Behaviour that must not change:** the payload-fallback queue. Every fallback trigger (HTTP 400, unknown model, rejected `response_format`) fires before generation starts, so it stays exactly as it is — but only until the first chunk arrives. After that, an error fails the request outright.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_summarizer.py`:

```python
def _sse_body(*contents: str) -> bytes:
    frames = [
        'data: {"choices":[{"delta":{"content":%s}}]}' % json.dumps(c) for c in contents
    ]
    frames.append("data: [DONE]")
    return ("\n".join(frames) + "\n").encode()


def test_chat_completion_streams_and_joins(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "http://llama.local/v1/chat/completions"
    sent: list[dict[str, object]] = []

    class StubStream:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self._body = body
            self.status_code = status

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return 200 <= self.status_code < 300

        async def aiter_lines(self) -> "AsyncIterator[str]":
            for line in self._body.decode().splitlines():
                yield line

        async def aread(self) -> bytes:
            return self._body

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            sent.append(json)
            return StubStream(_sse_body("Hel", "lo"))

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="sys",
            user_prompt="user",
        )
    )
    assert raw == "Hello"
    assert sent[0]["stream"] is True


def test_chat_completion_falls_back_on_error_before_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 still walks the payload queue — that behaviour is load-bearing."""
    labels: list[bool] = []

    class StubStream:
        def __init__(self, has_format: bool) -> None:
            self._has_format = has_format
            self.status_code = 400 if has_format else 200

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return self.status_code == 200

        async def aiter_lines(self) -> "AsyncIterator[str]":
            for line in _sse_body("ok").decode().splitlines():
                yield line

        async def aread(self) -> bytes:
            return b'{"error":{"message":"Unsupported parameter: response_format"}}'

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            has_format = "response_format" in json
            labels.append(has_format)
            return StubStream(has_format)

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    raw = asyncio.run(
        _client().chat_completion(
            model="Qwen2.5-7B-Instruct-Q4",
            system_prompt="sys",
            user_prompt="user",
        )
    )
    assert raw == "ok"
    assert labels[0] is True and labels[-1] is False  # tried with, then without


def test_chat_completion_does_not_retry_a_stream_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vts-94wf: an identical prompt cannot succeed on attempt two."""
    attempts = 0

    class StubStream:
        def __init__(self) -> None:
            self.status_code = 200

        async def __aenter__(self) -> "StubStream":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        @property
        def is_success(self) -> bool:
            return True

        async def aiter_lines(self) -> "AsyncIterator[str]":
            raise StreamTimeout("idle", chunks=3, elapsed=130.0)
            yield ""  # pragma: no cover - unreachable, satisfies the generator

        async def aread(self) -> bytes:
            return b""

    class StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "StubAsyncClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(self, method: str, url: str, json: dict[str, object]):
            nonlocal attempts
            attempts += 1
            return StubStream()

        async def get(self, url: str) -> httpx.Response:
            return _response(status_code=404, url=url, payload={}, method="GET")

    from vts.services.summarizer import StreamTimeout

    monkeypatch.setattr("vts.services.summarizer.httpx.AsyncClient", StubAsyncClient)

    with pytest.raises(RuntimeError):
        asyncio.run(
            _client().chat_completion(
                model="Qwen2.5-7B-Instruct-Q4",
                system_prompt="sys",
                user_prompt="user",
            )
        )
    assert attempts == 1, "a stream timeout must not be retried"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -k chat_completion_streams -v`
Expected: FAIL — the stub has no `post`, and `chat_completion` does not call `stream` yet

- [ ] **Step 3: Rewrite the request loop**

In `chat_completion`, add the keyword-only parameters after `num_ctx: int | None = None`:

```python
        stream_idle_timeout: float = 120.0,
        stream_first_chunk_timeout: float = 300.0,
        min_tokens_per_second: float = 3.0,
        ceiling_slack: float = 1.5,
        ceiling_floor_seconds: int = 300,
        ceiling_cap_seconds: int = 3600,
```

Add `"stream": True` inside `_build_chat_payload` (in `vts/services/summarizer.py`, next to the other unconditional keys):

```python
    payload["stream"] = True
```

Replace the body of the `while queue:` loop — everything from `label, payload = queue.pop(0)` down to the `data = response.json()` / `break` pair — with:

```python
                label, payload = queue.pop(0)
                ceiling = derive_stream_ceiling(
                    max_tokens,
                    min_tokens_per_second=min_tokens_per_second,
                    slack=ceiling_slack,
                    floor_seconds=ceiling_floor_seconds,
                    cap_seconds=ceiling_cap_seconds,
                )

                def _log_progress(chunks: int, elapsed: float) -> None:
                    rate = chunks / elapsed if elapsed > 0 else 0.0
                    logger.info(
                        "llm stream progress: %s chunks in %.0fs (%.1f chunks/s)",
                        chunks,
                        elapsed,
                        rate,
                    )

                try:
                    async with client.stream("POST", endpoint, json=payload) as response:
                        if not response.is_success:
                            body = await response.aread()
                            failures.append(
                                f"{label}: HTTP {response.status_code} ({body[:200]!r})"
                            )
                            if response.status_code != 400:
                                raise RuntimeError(
                                    f"llama chat completion failed for {endpoint}: {failures[-1]}"
                                )
                            if not discovered_model_fallback and model.strip():
                                discovered_model_fallback = True
                                available_models = await self._list_models(client=client)
                                if available_models and model not in available_models:
                                    server_model = available_models[0]
                                    enqueue(
                                        f"server_model:{server_model}",
                                        _build_chat_payload(
                                            **common,
                                            model_override=server_model,
                                            include_response_format=False,
                                        ),
                                    )
                            continue
                        text = await read_sse_stream(
                            response.aiter_lines(),
                            first_chunk_timeout=stream_first_chunk_timeout,
                            idle_timeout=stream_idle_timeout,
                            ceiling=ceiling,
                            on_progress=_log_progress,
                        )
                except StreamTimeout as exc:
                    # Deliberately no retry and no fallback: the prompt is
                    # byte-identical, so a second attempt cannot do better —
                    # it only burns the GPU (vts-94wf).
                    logger.warning(
                        "llm stream %s timeout: %s chunks in %.0fs",
                        exc.reason,
                        exc.chunks,
                        exc.elapsed,
                    )
                    raise
                except Exception as exc:
                    if _is_transient_http_error(exc):
                        failures.append(
                            f"{label}: {exc.__class__.__name__} ({str(exc).strip() or 'no details'})"
                        )
                        continue
                    raise
                return text
```

Delete the now-unreachable tail that unpacked `data["choices"][0]["message"]["content"]`, keeping the `else:` clause that raises when the queue empties.

- [ ] **Step 4: Run the new tests**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -k chat_completion -v`
Expected: PASS

- [ ] **Step 5: Run the whole summarizer suite for regressions**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py -v`
Expected: PASS — existing fallback tests must survive; if one fails because its stub only implements `post`, port that stub to `stream` rather than weakening the test.

- [ ] **Step 6: Commit**

```bash
git add vts/services/summarizer.py tests/test_summarizer.py
git commit -m "feat(llm): stream chat completions instead of one blocking POST

Closing the connection now stops generation on the backend. Measured
before this change: the client disconnected 20s in and Ollama kept going
for 17m38s, holding the GPU, then answered into a closed socket.

The payload-fallback queue is unchanged — every fallback trigger fires
before the first chunk. After the first chunk there is no fallback and no
retry: a stream timeout on a byte-identical prompt cannot succeed on a
second attempt.

Part of vts-gouq."
```

---

### Task 5: Wire settings through the pipeline

**Files:**
- Modify: `vts/pipeline/steps/summarization.py:522`, `:837`, `:1105`, `:1336` (the four `chat_completion` call sites)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: the keyword-only parameters from Task 4, the `Settings` fields from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
def test_stream_kwargs_helper_reads_settings() -> None:
    from vts.pipeline.steps.summarization import stream_kwargs

    class _S:
        llm_stream_idle_timeout_seconds = 90
        llm_stream_first_chunk_timeout_seconds = 200
        llm_min_tokens_per_second = 5.0
        llm_ceiling_slack_multiplier = 2.0
        llm_ceiling_floor_seconds = 100
        llm_ceiling_cap_seconds = 900

    assert stream_kwargs(_S()) == {
        "stream_idle_timeout": 90.0,
        "stream_first_chunk_timeout": 200.0,
        "min_tokens_per_second": 5.0,
        "ceiling_slack": 2.0,
        "ceiling_floor_seconds": 100,
        "ceiling_cap_seconds": 900,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_stream_kwargs_helper_reads_settings -v`
Expected: FAIL with `ImportError: cannot import name 'stream_kwargs'`

- [ ] **Step 3: Add the helper**

In `vts/pipeline/steps/summarization.py`, next to the existing `tokenizer_path` helper (around line 126):

```python
def stream_kwargs(settings: Settings) -> dict[str, float | int]:
    """Streaming limits for one `chat_completion` call.

    Collected in one place because all four call sites pass the same six
    values; threading them individually would be six chances to diverge.
    """
    return {
        "stream_idle_timeout": float(settings.llm_stream_idle_timeout_seconds),
        "stream_first_chunk_timeout": float(
            settings.llm_stream_first_chunk_timeout_seconds
        ),
        "min_tokens_per_second": float(settings.llm_min_tokens_per_second),
        "ceiling_slack": float(settings.llm_ceiling_slack_multiplier),
        "ceiling_floor_seconds": int(settings.llm_ceiling_floor_seconds),
        "ceiling_cap_seconds": int(settings.llm_ceiling_cap_seconds),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_summarizer.py::test_stream_kwargs_helper_reads_settings -v`
Expected: PASS

- [ ] **Step 5: Pass it at all four call sites**

At each of the four `ctx.llm.chat_completion(` calls, add as the last argument:

```python
                **stream_kwargs(ctx.settings),
```

Verify all four were changed:

```bash
grep -c "stream_kwargs(ctx.settings)" vts/pipeline/steps/summarization.py
```

Expected output: `4`

- [ ] **Step 6: Run the full suite**

```bash
sudo podman start vts-test-pg
./.venv/bin/python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add vts/pipeline/steps/summarization.py tests/test_summarizer.py
git commit -m "feat(llm): pass streaming limits from settings at every call site

One helper rather than six arguments threaded through four call sites,
so the values cannot drift apart.

Part of vts-gouq."
```

---

### Task 6: Document the settings and ship

**Files:**
- Modify: `vts/__init__.py` (version bump)
- Modify: `docs/LLM_BACKENDS.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Document the new settings**

Append to `docs/LLM_BACKENDS.md`:

```markdown
## Streaming timeouts

Completions are streamed, so closing the connection stops generation on the
backend rather than leaving it to finish into a dead socket.

Three limits guard a request, each answering a different question:

| YAML key (under `services.llm`) | default | meaning |
|---|---|---|
| `stream_first_chunk_timeout_seconds` | 300 | how long to wait for the first token — this covers model load, which is slow but healthy |
| `stream_idle_timeout_seconds` | 120 | the real stall detector: silence between tokens once text is flowing |
| `min_tokens_per_second` | 3 | slowest rate still considered progress |
| `ceiling_slack_multiplier` | 1.5 | headroom in the overall ceiling |
| `ceiling_floor_seconds` | 300 | lower bound on the ceiling |
| `ceiling_cap_seconds` | 3600 | upper bound on the ceiling |

The overall ceiling is `clamp(max_tokens / min_tokens_per_second * slack,
floor, cap)`. It scales with the work requested because a segment window
(~1255 tokens) and a final summary (~15000) differ by an order of magnitude;
one constant cannot serve both.

Neither timeout is retried. The prompt is byte-identical on every attempt, so
a request that could not finish once will not finish on the second try — it
only occupies the GPU.
```

- [ ] **Step 2: Bump the version**

In `vts/__init__.py`, increment the patch version (project rule: bump before committing).

- [ ] **Step 3: Run the full suite one more time**

```bash
./.venv/bin/python -m pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit and push**

```bash
git add docs/LLM_BACKENDS.md vts/__init__.py
git commit -m "docs(llm): document streaming timeout settings

Part of vts-gouq."
git push
```

- [ ] **Step 5: Verify against the live backend before tagging a build**

This is the one check the unit tests cannot make: that cancelling really does
stop the model. With the worker running a summarization task, cancel it and
watch the Ollama log for the request to close promptly:

```bash
sudo podman logs --tail 5 ollama 2>&1 | grep -E 'POST +"/api/(chat|generate)"'
```

Expected: the request closes within seconds of the cancel, not minutes.
Before this change the same test showed `200 | 17m38s`.

Note the prerequisite: this only works with LiteLLM **1.97.0 or newer**.
Under 1.83.0 the proxy stopped reading the upstream stream but never closed
it (upstream #30244), so the backend kept generating regardless.

---

## Self-Review

**Spec coverage:**

| spec section | task |
|---|---|
| Idle timeout | 1 (setting), 3 (enforcement) |
| First-chunk budget | 1, 3 |
| Overall ceiling + formula | 1 (`derive_stream_ceiling`), 4 (applied per request) |
| Six settings under `services.llm` | 1 (fields + aliases), 5 (threaded through), 6 (documented) |
| Fallback queue preserved | 4 (Step 3 keeps the queue; a test asserts it) |
| Contract unchanged (`-> str`) | 4 (signature untouched, four call sites only gain kwargs) |
| No retry on timeout | 4 (`StreamTimeout` re-raised, test asserts one attempt) |
| Retry on network errors | 4 (`_is_transient_http_error` branch kept) |
| Progress logging | 3 (`on_progress`), 4 (`_log_progress`) |
| Cancellation stops the model | 5 → Step 5 of Task 6 (live check; falls out of streaming) |
| Test list from the spec | 1-5, all seven cases present |

Spec items deliberately not implemented, matching its "out of scope": streaming
text to the browser, a user-facing Cancel button, and fixing `max_tokens` /
removing `cache_prompt` (the latter already landed in 1.7.37).

**Placeholder scan:** no TBD/TODO; every code step carries real code.

**Type consistency:** `derive_stream_ceiling` (Task 1) is called in Task 4 with
exactly its keyword names; `read_sse_stream` and `StreamTimeout` (Task 3) are
used in Task 4 with the signatures defined; `stream_kwargs` (Task 5) returns
precisely the six keyword-only parameters Task 4 adds.
