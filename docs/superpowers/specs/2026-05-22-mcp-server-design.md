# MCP Server for vts — Design

**Date:** 2026-05-22
**Status:** Draft, awaiting user review
**Beads:** vts-163

## Goal

Expose vts to MCP clients (Claude Desktop, Claude Code, etc.) so a user can:

- Submit a video URL for processing.
- List their tasks and see status.
- Fetch the raw transcript, the redacted (processed) transcript, and the
  summary.
- Wait for a task to reach a given stage (transcript ready / summary ready /
  done) without polling.

v1 is read + submit only. No destructive operations (pause/resume/delete/
restart) and no file uploads through MCP.

## Architecture

The MCP server lives **inside the existing webapi process**. A new package
`vts/mcp/` registers a [FastMCP](https://gofastmcp.com) 2.x server and the
FastAPI app mounts it as an ASGI sub-app at `/mcp` (configurable).

```
                 ┌──────────────────────────────────┐
   MCP client  ──▶│ reverse proxy (sets             │
   (Claude)       │  X-Forwarded-User)              │
                 └──────────────────────────────────┘
                            │
                            ▼
                 ┌──────────────────────────────────┐
                 │ FastAPI app (vts.api.main)       │
                 │   /api/...   (REST)              │
                 │   /mcp/...   (FastMCP, mounted)  │
                 └──────────────────────────────────┘
                            │
                            ▼
                 services / repo / Redis bus
                  (shared with REST)
```

### New code

- `vts/mcp/server.py` — constructs the `FastMCP` instance, registers tools,
  exposes `build_mcp_app() -> ASGIApp`.
- `vts/mcp/auth.py` — resolves `AuthenticatedUser` from the HTTP request
  attached to the MCP request context, reusing `vts.services.auth`
  unchanged.
- `vts/mcp/tools.py` — six tool implementations. Each tool delegates to
  existing service/repository functions; **no in-process HTTP self-calls**.

### Mount point

In `vts/api/main.py`, after the FastAPI app is built, mount only if
`settings.mcp_enabled` is true:

```python
if settings.mcp_enabled:
    app.mount(settings.mcp_path, build_mcp_app())
```

### Transport

Streamable HTTP (MCP-recommended). The same reverse-proxy chain that handles
`/api/*` handles `/mcp/*`; the same trusted-proxy CIDR check and auto-create-
user logic apply. No new auth path.

### Why FastMCP, not the raw `mcp` SDK

FastMCP gives us:

- `@mcp.tool` decorators that derive JSON Schema from Python type hints —
  same shape we already use for FastAPI request models.
- Built-in `streamable_http_app()` that mounts cleanly as an ASGI sub-app.
- Direct access to the underlying HTTP request inside a tool via the
  `Context` parameter, which we need for `X-Forwarded-User`.

The raw `mcp` SDK is rejected because it would mean reimplementing schema
generation and session management with no functional benefit. A bespoke
JSON-RPC-over-REST shim is rejected because it would not be a real MCP
server — clients would not connect.

## Tools (v1)

All tools require an authenticated user (`X-Forwarded-User`). Errors map to
MCP errors derived from `HTTPException` (401 missing/untrusted user, 404
task not found / artifact not ready, 422 validation).

### `submit_video`

Submit a URL (YouTube or anything `yt-dlp` accepts) for processing.

| Param | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | Same validation as the REST `POST /api/tasks`. |
| `title` | string | no | Optional override; otherwise yt-dlp metadata is used. |

Returns:

```json
{ "task_id": "uuid", "status": "queued", "created_at": "RFC3339" }
```

Behavior: identical to `POST /api/tasks` — fire-and-forget. The tool returns
as soon as the task is committed to `queued` and `notify_queued()` is
published.

### `list_tasks`

List the calling user's tasks.

| Param | Type | Default | Notes |
|---|---|---|---|
| `status` | enum | (none) | One of `queued`, `running`, `done`, `failed`, `paused`. Omitted = all. |
| `limit` | int | 20 | Capped at 100. |
| `sort` | enum | `updated_at` | `created_at` \| `updated_at` \| `title`. |
| `order` | enum | `desc` | `asc` \| `desc`. |

Returns array of:

```json
{
  "task_id": "uuid",
  "status": "running",
  "title": "string|null",
  "url": "string|null",
  "created_at": "RFC3339",
  "updated_at": "RFC3339"
}
```

`updated_at` is the existing `Task.updated_at` column. It is bumped by
`repo.set_task_status(...)` and by artifact-field updates. The tool
description documents this caveat: it is "last activity on the task,"
which is normally what an LLM caller wants when picking recent work.
A dedicated `status_changed_at` column is **not** introduced in v1.

### `get_status`

Get current status for one task.

Param: `task_id: string` (UUID).

Returns:

```json
{
  "task_id": "uuid",
  "status": "running",
  "stage": "transcribing|summarizing|...",
  "asr_progress": 0.42,
  "summary_progress": 0.0,
  "error": "string|null",
  "updated_at": "RFC3339"
}
```

Field set follows the existing `serialize_task(...)` output, trimmed to what
an external caller needs (no internal IDs, no queue positions).

### `get_transcript`

Fetch the transcript text.

| Param | Type | Default | Notes |
|---|---|---|---|
| `task_id` | string | — | UUID. |
| `variant` | enum | `raw` | `raw` (file at `task.transcript_path`) \| `redacted` (file at `<artifact_dir>/outputs/redacted_transcript.txt`). |

Returns:

```json
{ "task_id": "uuid", "variant": "raw", "content": "...", "format": "txt|json" }
```

`format` is derived from the file extension (`txt` or `json`). For
`variant=redacted`, format is always `txt`.

Errors: 404 if the variant is not ready yet.

### `get_summary`

Fetch the summary.

Param: `task_id: string`.

Returns:

```json
{ "task_id": "uuid", "content": "...", "format": "markdown" }
```

Errors: 404 if not ready.

### `wait_for_task`

Block until the task reaches a target stage, then return its current
snapshot.

| Param | Type | Default | Notes |
|---|---|---|---|
| `task_id` | string | — | UUID. |
| `until` | enum | `done` | `transcript` \| `summary` \| `done`. |
| `timeout_seconds` | int | 300 | Capped at 1800 (30 min). |

Returns:

```json
{
  "task_id": "uuid",
  "status": "done",
  "reached": true,
  "stage": "...",
  "updated_at": "RFC3339"
}
```

`reached: false` means the timeout fired before the target stage; the
snapshot still reflects the latest state.

Implementation (Redis-backed, gap-free):

1. Open a Redis pubsub on `{redis_prefix}events` and `subscribe(...)` **first**.
2. **Then** read the task from the DB. If the `until` condition is already
   satisfied (e.g. `task.summary_path` exists for `until=summary`, or
   `task.status in {done, failed}` for `until=done`), `unsubscribe` and
   return immediately.
3. Otherwise loop on `pubsub.get_message(timeout=...)` under
   `asyncio.wait_for(timeout_seconds)`:
   - Filter payloads by `user_id == current_user.id` **and** `task_id == requested`.
   - Exit on `event == "task_status"` with `status in {done, failed}`.
   - For `until=transcript`: exit when a `phase` event indicating the
     transcribe stage finished arrives (exact event name confirmed during
     implementation by reading `vts/pipeline/processor.py`). Fallback:
     re-check `task.transcript_path` on every wake-up — cheap.
   - For `until=summary`: same pattern keyed on the summary phase /
     `task.summary_path`.
4. `finally`: `unsubscribe` + `close` regardless of outcome.

The subscribe-then-check order is deliberate: any event published after our
`subscribe(...)` is buffered in the pubsub queue and will not be lost if
the DB read shows the task not yet ready. This mirrors how
`GET /api/events` consumes the same channel.

## Auth

`vts/mcp/auth.py` exposes `async def mcp_current_user(ctx: Context) -> AuthenticatedUser`:

- Pulls the HTTP request from `ctx.request_context.request` (FastMCP passes
  the underlying Starlette request through).
- Calls the existing `vts.services.auth.resolve_user(...)` with the same
  args as the REST `Depends(get_current_user)`: trusted-proxy CIDR check
  on the source IP, `X-Forwarded-User` header, auto-create on first request.
- Raises the same `HTTPException` shapes; FastMCP converts them to MCP
  errors.

No new auth code paths. The trusted-proxy config (`VTS_TRUSTED_PROXY_CIDRS`)
governs both REST and MCP.

## Configuration

New settings in `vts/core/config.py`:

| Key | Env | Default | Purpose |
|---|---|---|---|
| `mcp_enabled` | `VTS_MCP_ENABLED` | `True` | Mount the MCP sub-app on startup. |
| `mcp_path` | `VTS_MCP_PATH` | `/mcp` | Mount path. |

Both added to `.env.example` with comments.

No DB migrations. No new Redis channels or keys.

## Dependencies

Add to `requirements.txt`:

- `fastmcp>=2.0,<3` — exact pin chosen after the first compatibility check
  during implementation.

No removals.

## Testing

- **Unit (per tool):** call the underlying tool functions directly (without
  the MCP wrapper) against a test DB and the existing fakeredis fixture.
  Covers: `submit_video` happy path + invalid URL; `list_tasks` filters and
  sort; `get_status` for each pipeline state; `get_transcript` for both
  variants including the "not ready" 404; `get_summary` ready and not-ready;
  `wait_for_task` for all three `until` values, including the
  subscribe-then-check race scenario (publish an event between
  `subscribe()` and the DB read in a controlled way) and the timeout path.
- **Integration (one test):** boot the FastAPI test client with MCP
  mounted, perform an MCP streamable-HTTP handshake, call `submit_video`
  with a fake URL (downloader/whisper/llm mocked exactly as in existing
  pipeline tests), publish a synthetic `task_status=done` event into
  fakeredis, call `wait_for_task` with a short timeout, then
  `get_summary`. Asserts end-to-end shape, not pipeline correctness
  (pipeline tests already cover that).
- No real videos, no real Whisper, no real LLM in any MCP test.

## Documentation

- `README.md` — short "MCP" section with:
  - When to enable it.
  - Example `claude_desktop_config.json` snippet using `type: "http"` and
    the user's vts URL.
  - Tool list and one-line descriptions.
- `.env.example` — `VTS_MCP_ENABLED` and `VTS_MCP_PATH` entries.
- A dedicated `docs/MCP.md` only if the README section grows past ~60 lines.

## Out of scope (v1)

Explicitly **not** in this spec; tracked separately if needed later:

- `pause` / `resume` / `delete` / `restart_summary` / `archive` tools.
- File upload via MCP (only URL submission).
- Paging or truncation of large transcripts/summaries.
- Per-user API keys / Bearer-token auth (we lean on the existing proxy).
- Rate limiting beyond what the worker's heavy-slot already provides.
- A separate `status_changed_at` DB column.

## Open implementation questions

These are not blocking the spec; they get resolved while writing the
implementation plan or while coding.

1. Exact event/`phase` name(s) emitted by `vts/pipeline/processor.py` for
   "transcript finished" and "summary finished" — confirmed by reading the
   processor and grepping `event=` / `phase=`.
2. FastMCP version pin: highest stable 2.x that supports the
   `Context.request_context.request` accessor we rely on; verified during
   the first install.
