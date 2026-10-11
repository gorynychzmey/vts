"""One place that builds the Redis client, so every caller gets the same
tolerance for a connection that died while nobody was looking.

Prod, 2026-10-11: `GET /api/tasks` answered 500 four times with
`ConnectionError: … (104, 'Connection reset by peer')` on a cache read, while
Redis itself was up the whole time — PING 12 ms, 19 days uptime, server-side
`timeout 0`, `rejected_connections 0`. Nothing had closed the client: the flow
was reset in the network path between the app and the Redis host while it sat
idle, and the pool handed the dead socket to the next request.

A client built with bare `from_url()` cannot notice that. redis-py does not
retry `ConnectionError` by default and does not probe a connection it has been
holding, so the first command after the reset is the one that fails — in
whichever request happens to arrive first.

The three settings below are what make that invisible rather than fatal:

* `health_check_interval` — PING a connection idle longer than this before
  reusing it, and reconnect when the PING fails.
* `socket_keepalive` — ask the kernel to keep the flow alive so it is less
  likely to be dropped in the first place.
* `retry` + `retry_on_error` — one immediate reconnect-and-retry when a command
  does hit a dead socket, which covers the window between two health checks.

Callers that treat Redis as a cache must STILL degrade on `RedisError`: this
narrows the window, it does not close it. See `_get_cached_queue_positions`.
"""
from __future__ import annotations

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

# Shorter than the 300s keepalive the server sends, so a flow dropped between
# two of its probes is still caught on our side before a command rides it.
HEALTH_CHECK_SECONDS = 30


def make_redis(url: str, *, decode_responses: bool = False) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=decode_responses,
        health_check_interval=HEALTH_CHECK_SECONDS,
        socket_keepalive=True,
        retry=Retry(NoBackoff(), 1),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
