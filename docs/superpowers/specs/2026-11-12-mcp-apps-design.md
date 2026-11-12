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
| 1 | `ui://vts/player` — media + transcript, click-to-seek, current line highlighted, text filter | **new** `show_recording(recording_id, at_sec?)` | `get_recording_transcript(structured=true)` pages + signed media URL |
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

## 4. Server side

### 4.1 Registration

Use `fastmcp.apps.FastMCPApp`, added to the existing server with
`server.add_provider(app)`. It declares the extension capability, serves the
`ui://` resources with MIME `text/html;profile=mcp-app`, and wires entry tools
(`visibility=["model"]`) and backend tools (`visibility=["app"]`).

Registration does **not** depend on whether the host announced the extension:
a host without Apps sees the entry tools as ordinary tools and ignores the
resources.

### 4.2 Resources

- One self-contained HTML file per view (JS and CSS inlined, single-file
  bundle), no external CDN, fonts or libraries.
- Static template: data arrives over the bridge, never baked into the HTML,
  so hosts can cache and pre-check it.
- `_meta.ui.csp`:
  - `resourceDomains`: the media origin only, derived at startup from
    `public_base_url` (no hard-coded host; self-hosted deployments differ).
  - `connectDomains`: empty — all data goes through tools.
- `prefersBorder: true`.

Open choice (§8): hand-written HTML bundle vs FastMCP's `@app.ui()` with
server-side components.

### 4.3 Backend (app-only) tools

| Tool | Purpose |
|---|---|
| `app_transcript_page(recording_id, cursor?, variant?)` | next page of `entries` for the player |
| `app_media_url(recording_id)` | issue a short-lived signed media URL |
| `app_prompt_result(recording_id, ref)` | summary for another prompt (regenerate view) |

All of them run the same per-user authorization as model-visible tools; the
server never trusts the view.

### 4.4 Result shape of entry tools

- `content`: short text for the model and for hosts without a view —
  title, duration, people, and the first lines or the line at `at_sec`.
- `structuredContent`: just enough to render the first screen (metadata and
  the first page of entries, or the window around `at_sec`), plus a cursor.

Do not put a whole transcript in `structuredContent`. Whether a host shows
`structuredContent` to the model varies by host; a 600-line transcript there
could flood the context. Large data is fetched by the view through
`app_transcript_page`. **To verify on Claude and ChatGPT before release.**

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

No private network or tunnel is assumed: the media origin is the same public
origin the MCP endpoint is already served from. A deployment without a public
origin simply gets the transcript-only fallback.

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
- PII: entry tools default to `variant="redacted"` for `content` when
  redaction is enabled for the recording; raw text only in the view and only
  on explicit request.
- Destructive actions are not part of the first views. If added, they reuse
  `annotations.DESTRUCTIVE` and confirm in the view.
- Prompt injection: transcripts are data; nothing in them is executed.

## 8. Open questions

1. View technology: hand-written single-file HTML bundles (full control,
   needs a JS build step in a Python repo) vs FastMCP `@app.ui()` components
   (no JS build, less control over a media player).
2. Where the view sources and their build live (`vts/mcp/ui/` with a small
   bundler step in `build.sh`, or prebuilt artefacts committed).
3. Does the player reuse `_load_player_blocks` (richer: blocks + sentences)
   or the simpler `TranscriptEntry` list? The former matches the web player;
   it is task-scoped today and would need a recording-scoped variant.
4. Is media through MCP (base64 chunks) worth it for deployments without a
   public origin, or is the transcript-only fallback enough?

## 9. Testing

- Unit: `structuredContent` shape, paging cursors, URL signing (expired,
  foreign recording, foreign user, tampered signature).
- Contract: `resources/read` returns the right MIME and CSP; every entry tool
  carries a `resourceUri` that resolves; app-only tools are hidden from
  `tools/list` for the model.
- Unchanged behaviour: existing tools' results are byte-for-byte the same.
- E2E: Playwright against the FastMCP apps dev host — render, click a line,
  media seeks; media missing → fallback shown.

## 10. Implementation steps

1. `FastMCPApp` + `ui://vts/player` + `show_recording`, transcript only.
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
