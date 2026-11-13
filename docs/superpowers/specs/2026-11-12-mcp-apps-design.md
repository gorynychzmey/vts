# MCP Apps: interactive views for the VTS MCP server — design

Status: draft · Spec: MCP Apps (SEP-1865), revision 2026-01-26 ·
Extension id: `io.modelcontextprotocol/ui`

## 1. Goal

Let MCP hosts that implement MCP Apps (Claude web/Desktop/mobile, ChatGPT,
VS Code and others) render interactive views of VTS data inline in the chat:
a transcript player first, then search results that jump into it.

**Tools stay primary.** A view is an addition on top of a tool that already
works without it. A host without MCP Apps support must see exactly what it
sees today; no existing tool changes its `content` or its result schema.

## 2. What VTS already has (and the views build on)

| Need | Already in VTS |
|---|---|
| MCP server, Streamable HTTP, OAuth, per-user auth | `vts/mcp/server.py` (FastMCP), mounted at `settings.mcp_path` in `vts/api/main.py` |
| MCP Apps support in the SDK | `fastmcp>=3.3` (pinned in `requirements.txt`) ships `fastmcp.apps`: `FastMCPApp`, `AppConfig(resource_uri=…, visibility=[…])`, app-only backend tools, and a dev host (`fastmcp dev apps`) |
| The lasting object to show | Recordings (`RecordingInfo`): they survive the deletion of the task that produced them, and carry `has_transcript` / `has_summary` / `has_media` |
| Timed, speaker-attributed transcript | `get_recording_transcript(structured=true)` → `entries[]` (`start_sec`, `end_sec`, `text`, `speaker`), windowed with `around_sec` / `window_sec`; `variant` = `raw` / `redacted` |
| Rich two-level transcript for a player | `_load_player_blocks()` (`vts/api/_helpers/artifact_store.py`) behind `/api/tasks/{id}/transcript-entries`: blocks with a resolved speaker label and clickable sentences |
| A working web player | `/player/{task_id}` (autoscroll, click-to-seek) |
| Media bytes | `GET /api/tasks/{id}/media` (`FileResponse`), task-scoped, session/bearer auth |
| Search with positions | `search_transcripts` → `SearchHit` (`recording_id`, `start_sec`, `end_sec`, `speakers`, `score`, `player_url`), with an explicit relevance threshold and `total` |
| Summaries and other prompt results | `get_prompt_result(task_id, ref)` / `get_recording_prompt_result` |
| Task status and waiting | `list_tasks`, `get_status`, `wait_for_task` |
| People | `list_people` (voice registry) |
| Confirm-before-delete hints | `vts/mcp/annotations.py` (`DESTRUCTIVE` sets `destructiveHint`) |
| Public origin for links | `settings.public_base_url` |

## 3. Views

| # | View (`ui://` resource) | Entry tool (model-visible) | Data source |
|---|---|---|---|
| 1 | `ui://vts/player` — media + transcript, click-to-seek, current line highlighted | **new** `show_recording(recording_id, at_sec?)` | `get_recording_transcript(structured=true)` pages + signed media URL |
| 2 | `ui://vts/search` — hits list, open hit in the player at `start_sec` | `search_transcripts` (existing; only `_meta.ui` added) | existing `SearchResult` |
| 3 | `ui://vts/summary` — rendered summary, regenerate with another prompt | **new** `show_summary(recording_id, ref?)` | `get_recording_prompt_result` |
| 4 | `ui://vts/speakers` — map labels to people, listen to a voice sample | later, depends on the speaker registry tools | to be designed |

Order: **1 → 2 → 3 → 4**. Player and search together give the complete
"find it → watch that moment" loop, and search needs no new data. A job-queue
view is dropped: `wait_for_task` already covers waiting for a result in chat,
and status is a one-line answer, not a UI.

### Why new entry tools instead of decorating `get_transcript`

`get_transcript` / `get_recording_transcript` return the full transcript in
`content`; existing clients depend on that. Attaching a view to them would
either change their output (breaking) or render a player for every
programmatic read (noise). A dedicated `show_*` tool says "the user wants to
look at this", returns a short text summary for the model, and leaves the read
tools untouched. `search_transcripts` is the exception: its result already is
what the view shows, so it only gains `_meta.ui.resourceUri`.

Done (vts-vtma.4): the search view opens a passage in the player INSIDE the
view — it calls `show_recording` itself through the bridge and mounts the same
`VtsPlayerView` as `ui://vts/player` — rather than asking the model to call a
tool. A late answer for a passage the user already left is dropped.

Done in part (vts-vtma.5): `show_summary` + `ui://vts/summary`, read-only,
with a switcher between the recording's finished prompt results and a small
DOM-building markdown renderer (no innerHTML). Regeneration (vts-vtma.8,
decided 2026-11-13: "as in the web UI, all prompts at once"):
`regenerate_summary` calls the same service as the web UI's "restart the final
summary" with a new set (`restart_final_with_prompts`), which clears all
existing results first. The view preselects the task's current set, confirms
with a warning, polls `get_status` and reloads `show_summary` when the task
completes. Not offered when the recording's task was deleted.

## 4. Server side

### 4.1 Registration

Plain FastMCP decorators in `vts/mcp/tools_registry/apps.py`, not
`FastMCPApp`: `@mcp.resource(uri, mime_type=UI_MIME_TYPE, app=AppConfig(...))`
for the view and `@mcp.tool(app=AppConfig(resource_uri=...))` /
`AppConfig(visibility=["app"])` for the tools. FastMCP already announces the
`io.modelcontextprotocol/ui` extension in the server capabilities. App-only
tools stay in `tools/list` carrying `visibility: ["app"]`, and the host keeps
them from the model — which is what the spec asks; `FastMCPApp` instead hides
its backend tools from the list and renames them with a hash prefix.

Registration does **not** depend on whether the host announced the extension:
a host without Apps sees the entry tools as ordinary tools and ignores the
resources.

### 4.2 Resources

- One self-contained HTML file per view, no external CDN, fonts or
  libraries. Built the way the web player already is: real `.html` / `.css` /
  `.js` files inlined into one page when the resource is read (see
  `_template()` in `vts/api/routers/artifacts.py`). No JS bundler, no
  `package.json` in the build.
- Static template: data arrives over the bridge, never baked into the HTML,
  so hosts can cache and pre-check it.
- `_meta.ui.csp`:
  - `resourceDomains`: the media origin only, derived at startup from
    `public_base_url` (no hard-coded host; self-hosted deployments differ).
  - `connectDomains`: empty — all data goes through tools.
- `prefersBorder: true`.

### 4.2.1 View technology (decided)

Vanilla JS, assembled like the web player (option C). Rejected:

- **Vite/esbuild bundle + `@modelcontextprotocol/ext-apps`** — adds a JS
  toolchain (node in `build.sh` and CI, a built artefact to embed) to a Python
  repo for the sake of a small bridge.
- **FastMCP `@app.ui()` (Prefab components)** — needs the extra `prefab-ui`
  dependency, and a media player synced to timecodes does not fit a generic
  component set.

The `ui/*` bridge (JSON-RPC over `postMessage`: `ui/initialize`,
`ui/notifications/tool-input`, `ui/notifications/tool-result`, `tools/call`,
`ui/update-model-context`) is written by hand, in one file shared by all
views. The risk that it drifts from the spec is covered by the E2E test
against `fastmcp dev apps`, an independent host implementation.

### 4.2.2 Shared player core (decided)

Click-to-seek, current-line highlight, autoscroll and positioning on a cited
moment moved out of `vts/api/_templates/player.js` into
`player_core.js`, which knows nothing about where data comes from. (The web
player has no text filter; one would be a new feature of the core, not part
of the move.) Two thin adapters feed it:

- web player (`/player/{task_id}`): `fetch` + the existing SSE live updates;
- MCP view (`ui://vts/player`): the bridge (`tool-result`, `tools/call`).

So the two players cannot drift apart in behaviour.

Done (vts-vtma.1). `tests/ui/scenarios/player-behaviour.mjs` pinned the
behaviour first — click and keyboard seek, highlight, autoscroll and its
pause, deep link, live rebuild, unavailable state, localised label — each
check proven against a broken player, and stayed unchanged and green across
the move. The core is also the page's only renderer now: the server embeds
the blocks as JSON. The two renderers it replaced disagreed on which
sentences to skip, so a live rebuild renumbered `data-cue` under a deep link
(`player-rebuild-numbering.mjs` fails on the old code).

### 4.2.3 Transcript shape (decided)

The view uses the same blocks as the web player: `_load_player_blocks()`
(blocks per ASR segment / speaker turn, a resolved speaker label, clickable
sentences with their own timecodes). One shape for both players is what makes
the shared core possible.

The view addresses recordings, so the loader is split (done, vts-vtma.1) into
`player_blocks_from_artifacts(artifact_dir, language, names)` and two
callers, `_load_player_blocks(task)` and `load_recording_player_blocks()`:

- task caller: as now;
- recording caller: `recording.artifact_dir`, `recording.language`, names via
  `recording.source_task_id` — empty once the task is deleted (voice
  identification belongs to the task; same rule as `RecordingInfo.people`),
  in which case labels fall back to the plain speaker word.

The player shows the raw transcript, like the web player does; `variant`
applies to the model-facing `content` only (§7).

### 4.3 Backend (app-only) tools

| Tool | Purpose |
|---|---|
| `app_player_blocks(recording_id, cursor?)` | next page of player blocks (§4.2.3) |
| `app_media_url(recording_id)` | issue a short-lived signed media URL |
| `app_prompt_result(recording_id, ref)` | summary for another prompt (regenerate view) |

All of them run the same per-user authorization as model-visible tools; the
server never trusts the view.

### 4.4 Result shape of entry tools

- `content`: short text for the model and for hosts without a view —
  title, duration, people, how many timed sentences, and a pointer to
  `get_recording_transcript`. No transcript text: the reading tools carry
  their own redaction choice, and the view shows the text to the user.
- `structuredContent`: just enough to render the first screen (metadata and
  the first page of player blocks, or the page holding `at_sec`), plus a
  cursor.

Do not put a whole transcript in `structuredContent`. Whether a host shows
`structuredContent` to the model varies by host; a 600-line transcript there
could flood the context. Large data is fetched by the view through
`app_player_blocks`. **To verify on Claude and ChatGPT before release.**

## 5. Media

The view runs in a sandboxed iframe on the host's origin, so it carries no VTS
session cookie and cannot send a bearer token with a `<video src>`.
`/api/tasks/{id}/media` is therefore unreachable from it as is.

Needed:

1. **A recording-scoped media route.** Media is only exposed per task today;
   the player addresses recordings (they outlive tasks). Add
   `GET /api/recordings/{id}/media`, resolving the file the same way
   `RecordingInfo.has_media` does.
2. **Signed URLs.** `app_media_url` returns
   `{public_base_url}/m/{recording_id}?exp=…&sig=…`: HMAC over
   `(recording_id, user_id, exp)`, `exp` ≤ 10 min, key derived from a server
   secret (same approach as the session middleware key, separate purpose
   label). The route checks the signature and expiry instead of a session and
   must support `Range` (seeking).
3. **Fallback.** No media (`has_media=false`), expired link or unreachable
   origin → the player hides itself and the transcript stays usable.

Done (vts-vtma.3): `GET /api/recordings/{id}/media?u=&exp=&sig=` with no
session; the key is HMAC(session secret, "vts media url v1"), so no new
secret to configure; an `exp` further out than one TTL is refused even when
correctly signed; every refusal is the same 404. The view renews an expired
link each time it expires, and gives up (player removed, transcript kept)
only when a just-renewed link fails before loading — no renew loop.

No private network or tunnel is assumed: the media origin is the same public
origin the MCP endpoint is already served from.

**Deployments without `public_base_url` (checked against the code).** The MCP
server is HTTP-only (mounted into the FastAPI app, no stdio transport).
`public_base_url` is mandatory only with `oauth_enabled`
(`build_mcp_server()` refuses to start without it). Without OAuth, a personal
API token (`Bearer vts_…`) authenticates MCP calls, so a local install with no
public origin is possible — e.g. Claude Desktop or VS Code pointed at
`http://localhost:…/mcp`. The hosted surfaces (claude.ai web/mobile, ChatGPT)
cannot reach such an install at all, so for them the question does not arise.

Decision: media URLs are built from `public_base_url` only; when it is unset,
`app_media_url` returns nothing and the player runs transcript-only. No
"media over MCP" (base64 chunks) mode: it would serve only the local-install
case, where the web player at the same address already plays the media.
Deriving the origin from the incoming request instead is rejected: behind a
proxy it yields an internal host, and the CSP would then differ per request,
defeating a static, cacheable resource.

## 6. View behaviour

Lifecycle: host loads the `ui://` resource → `ui/initialize` handshake →
host sends tool input and result → view renders and calls backend tools via
the host → view may update the model's context
(`ui/update-model-context`, e.g. "user selected 12:40–14:10: …") so the user
can say "summarise this part".

Requirements:

- light/dark theme from the host context;
- narrow layout: transcript below the player;
- UI strings in en/ru/de, reusing the web UI's i18n keys where they exist;
- transcript text is untrusted: escaped, never interpreted as HTML or
  instructions;
- build only on the standard `ui/*` bridge (`@modelcontextprotocol/ext-apps`),
  no host-specific APIs (`window.openai`), so one view runs in every host.

## 7. Security

- Auth: the existing MCP OAuth; every backend call is checked against the
  calling user like any tool call.
- Media URLs: signed, short-lived, bound to recording and user, issued only by
  an app-only tool.
- PII: entry tools use `variant="redacted"` for the model-facing `content`
  when a redacted transcript exists for the recording. The view, like the web
  player, shows the raw transcript to its owner.
- Destructive actions are not part of the first views. If added, they reuse
  `annotations.DESTRUCTIVE` and confirm in the view.
- Prompt injection: transcripts are data; nothing in them is executed.

## 8. Decisions taken (2026-11-12)

1. View technology: vanilla JS assembled like the web player (§4.2.1).
2. View sources: `vts/mcp/ui/` (per-view `.html` / `.css` / `.js`, plus the
   shared bridge); the shared player core lives where both the web player and
   the MCP resource can inline it (§4.2.2).
3. Transcript shape: `_load_player_blocks()`, split for recordings (§4.2.3).
4. No media over MCP; no `public_base_url` → transcript-only (§5).

## 9. Testing

- Unit: `structuredContent` shape, paging cursors, URL signing (expired,
  foreign recording, foreign user, tampered signature).
- Contract: `resources/read` returns the right MIME and CSP; every entry tool
  carries a `resourceUri` that resolves; app-only tools are hidden from
  `tools/list` for the model.
- Unchanged behaviour: existing tools' results are byte-for-byte the same.
- E2E: `tests/ui/scenarios/mcp-player-view.mjs` plays the host — loads the
  real view in a sandboxed iframe, speaks the `ui/*` protocol to it, records
  what it sends (handshake order, page requests, model context, size).
  `fastmcp dev apps` was the plan but needs the `prefab-ui` extra and an
  unauthenticated server; checking in a real host (Claude, ChatGPT) after
  deploy replaces it.

## 10. Implementation steps

0. Pin the web player's behaviour with `tests/ui` scenarios, then extract
   the shared core with those scenarios unchanged and green. Split `_load_player_blocks()` for recordings.
1. `FastMCPApp` + bridge + `ui://vts/player` + `show_recording`, transcript
   only.
2. Recording-scoped media route + signed URLs + player sync.
3. `ui://vts/search` on `search_transcripts`, opening hits in the player.
4. `ui://vts/summary` + `show_summary` + regenerate.
5. `ui://vts/speakers`, once the speaker tools for it exist.

## 11. Host support

MCP Apps is an open MCP extension, not Claude-specific. Claude and ChatGPT
both implement it (ChatGPT additionally offers optional `window.openai` APIs,
which these views do not use). Hosts or workspaces that disable UI rendering
get the tools' text results only.

## References

- Spec: https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- SEP-1865: https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp
- MCP Apps in ChatGPT: https://developers.openai.com/apps-sdk/mcp-apps-in-chatgpt
- Interactive connectors in Claude: https://support.claude.com/en/articles/13454812-using-interactive-connectors-in-claude
