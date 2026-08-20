"""The SSE stream must end when the app shuts down (vts-9er).

Before this fix the generator looped forever on pubsub, so uvicorn's graceful
shutdown blocked until --timeout-graceful-shutdown expired and the container
was SIGKILLed. Measured on the live host: `podman stop -t 15 vts-webapi` waited
the full 15s, with uvicorn logging "Waiting for connections to close."
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest


class _FakePubSub:
    """Just enough pubsub for the SSE generator: it subscribes, polls for
    messages, and unsubscribes on the way out. Never yields a message, which
    is exactly the idle stream that used to block shutdown."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        # Behave like a real idle channel: block for the timeout, return None.
        await asyncio.sleep(timeout)
        return None

    async def unsubscribe(self, channel: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _FakeRedis:
    """app.state.redis normally comes from the lifespan, which the
    ASGITransport-based test client never runs."""

    def __init__(self) -> None:
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self) -> _FakePubSub:
        ps = _FakePubSub()
        self.pubsubs.append(ps)
        return ps


class _FakeRequest:
    """Stands in for the Starlette Request the endpoint now takes.

    The generator polls `is_disconnected()` to notice uvicorn closing the
    connection — which is what actually unblocks a graceful shutdown, since
    uvicorn waits for connections BEFORE running the lifespan.
    """

    def __init__(self, disconnected: bool = False, app=None) -> None:
        self._disconnected = disconnected
        # The handler reads the shutdown flag off `request.app.state` (it lives
        # in a router now, so there is no enclosing `app` to close over).
        self.app = app

    async def is_disconnected(self) -> bool:
        return self._disconnected


async def _call_events_endpoint(app):
    """Invoke the /api/events handler directly, bypassing the transport.

    The endpoint lives in vts.api.routers.tasks; it is reached through the
    router rather than imported. Dependencies are supplied by hand because we
    are not going through FastAPI's dependency injection here.
    """
    from vts.core.config import get_settings
    from vts.services.auth import AuthenticatedUser

    route = next(
        r for r in app.router.routes
        if getattr(r, "path", None) == "/api/events"
    )
    user = AuthenticatedUser(
        id="00000000-0000-0000-0000-0000000000a1",
        username="tester",
        requested_by="tester",
        is_admin=False,
        acting_as="tester",
    )
    return await route.endpoint(
        request=_FakeRequest(app=app), user=user, redis=app.state.redis, settings=get_settings()
    )


@pytest.mark.asyncio
async def test_event_stream_ends_when_shutdown_is_signalled(client, authed_app):
    """Setting the shutdown event must end the stream promptly."""
    app, _factory = authed_app

    # Both of these normally come from the lifespan, which does not run here.
    app.state.redis = _FakeRedis()
    app.state.shutting_down = asyncio.Event()

    frames: list[str] = []

    async def read_stream() -> None:
        async with client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                frames.append(line)

    reader = asyncio.create_task(read_stream())
    try:
        # Give the generator time to subscribe and emit its first frame.
        await asyncio.sleep(0.5)
        app.state.shutting_down.set()

        # Must finish quickly — the whole point is not waiting out a timeout.
        await asyncio.wait_for(reader, timeout=10)
    finally:
        if not reader.done():
            reader.cancel()

    body = "\n".join(frames)
    assert "event: server_shutdown" in body, body
    # The generator's finally must still run, or the subscription leaks.
    assert app.state.redis.pubsubs, "the generator never subscribed"
    assert app.state.redis.pubsubs[0].closed, "pubsub was not closed on exit"


@pytest.mark.asyncio
async def test_event_stream_still_starts_without_the_flag(client, authed_app):
    """A missing app.state.shutting_down must not break the endpoint.

    Defensive: the attribute only exists once the lifespan has run, so the
    generator must not raise if something constructs the app differently.

    Driven through the endpoint directly rather than the test client, because
    httpx's ASGITransport does NOT stream — it awaits `response_complete`
    and buffers the whole body (verified by reading its source). A stream that
    never ends therefore cannot be consumed through it at all; only the first
    test can use the client, and only because its stream does end.
    """
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    if hasattr(app.state, "shutting_down"):
        delattr(app.state, "shutting_down")

    response = await _call_events_endpoint(app)
    agen = response.body_iterator
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=5)
    finally:
        await agen.aclose()

    assert first.startswith("event: server_version"), first


@pytest.mark.asyncio
async def test_event_stream_ends_when_the_client_disconnects(client, authed_app):
    """A disconnected client must end the stream — this is what actually
    unblocks uvicorn's graceful shutdown.

    uvicorn calls connection.shutdown() on every open connection and THEN
    waits for them; only afterwards does it run the lifespan. So the lifespan
    event alone arrives too late: measured on the live host, a stop still took
    the full 15s with only that check in place. Noticing the disconnect is
    what makes the stop prompt.
    """
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    app.state.shutting_down = asyncio.Event()  # never set: disconnect must suffice

    from vts.core.config import get_settings
    from vts.services.auth import AuthenticatedUser

    route = next(
        r for r in app.router.routes if getattr(r, "path", None) == "/api/events"
    )
    request = _FakeRequest(disconnected=False, app=app)
    response = await route.endpoint(
        request=request,
        user=AuthenticatedUser(
            id="00000000-0000-0000-0000-0000000000a1",
            username="tester",
            requested_by="tester",
            is_admin=False,
            acting_as="tester",
        ),
        redis=app.state.redis,
        settings=get_settings(),
    )
    agen = response.body_iterator

    # First frame proves the stream is live.
    first = await asyncio.wait_for(agen.__anext__(), timeout=5)
    assert first.startswith("event: server_version"), first

    # Now hang up. The generator polls is_disconnected once a second, so it
    # must finish well inside the 30s pubsub read it would otherwise sit in.
    request._disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=10)

    assert app.state.redis.pubsubs[0].closed, "pubsub was not closed on disconnect"


@pytest.mark.asyncio
async def test_sigterm_sets_the_shutdown_flag_before_the_lifespan(authed_app):
    """SIGTERM must set app.state.shutting_down at signal-delivery time.

    This is the whole fix for the webapi half of vts-9er. uvicorn's
    Server.shutdown() waits for open connections BEFORE running the lifespan:

        connection.shutdown() for each connection
        await asyncio.wait_for(self._wait_tasks_to_complete(), timeout=...)  <- waits here
        await self.lifespan.shutdown()                                       <- flag was set here

    So a flag set by the lifespan arrives after the wait it was meant to cut
    short, and an idle SSE stream held the stop for the full
    --timeout-graceful-shutdown (measured: 15s on the live host, twice).

    uvicorn installs its own handler with plain signal.signal (server.py:319),
    and that handler only flips `should_exit`. We chain ours in front of it,
    which fires at signal delivery — before shutdown() is even entered.
    Measured on an isolated harness with one live SSE client: 15.19s without
    the chained handler, 0.19s with it.
    """
    import signal

    app, _factory = authed_app

    # The fixture builds the app but never enters the lifespan, and the
    # handler is installed on the way in — so drive the lifespan here, which
    # is also the path uvicorn actually takes.
    before = signal.getsignal(signal.SIGTERM)
    async with app.router.lifespan_context(app):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), (
            "no SIGTERM handler installed; the app must register one at startup"
        )
        assert handler is not before, "the previous handler was not replaced"
        assert not app.state.shutting_down.is_set()

        # Deliver the signal the way the kernel would, then let the loop run
        # the call_soon_threadsafe the handler schedules.
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0)

        assert app.state.shutting_down.is_set(), (
            "SIGTERM did not set the shutdown flag; the SSE stream would keep "
            "uvicorn waiting for the full graceful-shutdown timeout"
        )

    # On the way out the signal must be handed back, so uvicorn's own restore
    # in capture_signals() puts back what was there before the app started.
    assert signal.getsignal(signal.SIGTERM) is before, (
        "the previous SIGTERM handler was not restored"
    )


class _MessagePubSub(_FakePubSub):
    """A channel that delivers one message, then blocks like an idle one.

    The message is what gets the generator to a `yield`; the block afterwards
    is what leaves a pending `get_message` future behind when the consumer
    walks away mid-stream.
    """

    def __init__(self) -> None:
        super().__init__()
        self._delivered = False

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        if not self._delivered:
            self._delivered = True
            import json as _json

            payload = {"user_id": "00000000-0000-0000-0000-0000000000a1", "event": "task_updated"}
            return {"data": _json.dumps(payload).encode("utf-8")}
        await asyncio.sleep(timeout)
        return None


@pytest.mark.asyncio
async def test_cancelling_the_stream_cancels_the_in_flight_pubsub_read(authed_app):
    """A client that hangs up must not strand the in-flight pubsub read.

    The loop parks in `asyncio.wait` with a fresh `get_message` future
    pending. When the client disconnects, Starlette cancels the task running
    the generator, and the CancelledError lands inside that wait. The existing
    `finally` cancels `stop` and `gone` — but not `read`, which is left owned
    by nobody. It later fails with a redis ConnectionError that asyncio reports
    as "Task exception was never retrieved": recurring log noise on prod, and
    one leaked future per disconnect (vts-9tr3).

    Note the read must be cancelled from inside the generator. Cancelling the
    consuming task does not reach it: `ensure_future` makes an independent
    task, not a child of whoever awaited it.
    """
    app, _factory = authed_app
    _pubsub = _FakePubSub()
    app.state.redis = _FakeRedis()
    app.state.redis.pubsub = lambda: _pubsub  # type: ignore[method-assign]
    app.state.shutting_down = asyncio.Event()

    response = await _call_events_endpoint(app)
    agen = response.body_iterator

    await agen.__anext__()  # server_version, then the loop starts a read

    # Park the generator inside asyncio.wait, then hang up on it.
    pump = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    pump.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pump
    with contextlib.suppress(RuntimeError):
        await agen.aclose()
    await asyncio.sleep(0.05)

    leaked = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and not t.done()
        and "get_message" in repr(t.get_coro())
    ]
    assert not leaked, f"leaked pubsub read(s): {leaked}"
