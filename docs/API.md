# Programmatic access

VTS exposes its full REST API as an OpenAPI 3.x spec. Use it from scripts
(curl, Python), from generated client libraries, or as a **ChatGPT Custom
Action**.

## Endpoints

| URL | Purpose |
|-----|---------|
| `GET /openapi.json` | Machine-readable OpenAPI 3.x spec |
| `GET /docs` | Swagger UI — interactive browser for the same spec |
| `GET /redoc` | ReDoc rendering of the same spec |

All three work on any deployment without extra setup. `/openapi.json`
includes a `servers` block when `VTS_PUBLIC_BASE_URL` is set, so the spec
is self-contained (absolute URLs, ready to import into other tools).

## Authentication

External clients use **personal API tokens**, not OAuth — generate one
in the UI (header → key icon → Create token), then send it as:

```
Authorization: Bearer vts_<...>
```

The spec advertises this as `securitySchemes.ApiToken` (HTTP Bearer)
applied globally; `/api/version` and a couple of other public endpoints
opt out.

See [docs/AUTH.md](AUTH.md#personal-api-tokens) for the full auth model
— allow-list semantics, revocation, scope (= owner's full rights, no
permission system).

## ChatGPT Custom Action

[OpenAI's Custom Actions](https://platform.openai.com/docs/actions) and
GPT Builder both speak OpenAPI 3.x natively.

1. **Create the GPT / assistant** in ChatGPT or the OpenAI dashboard.
2. **Import the schema** — paste `<your-vts>/openapi.json` as the URL,
   or copy the JSON body.
3. **Configure auth** in the Action settings:
   - Authentication type: **API key**
   - Custom header name: leave blank (Bearer is auto-handled)
   - API key value: your `vts_…` token
   - Auth type: **Bearer**
4. **Test** the action — ChatGPT will list every exposed endpoint
   (tasks, transcripts, summaries, admin) with the right request bodies
   and response shapes derived from VTS' Pydantic models.

What gets exposed (everything under `/api/tasks`, `/api/recordings`,
`/api/search`, `/api/me`, `/api/admin`, `/api/version`):

- `POST /api/tasks` — submit by URL
- `POST /api/tasks/upload` — upload a file (multipart)
- `GET /api/tasks` — list
- `GET /api/tasks/{id}` — status / progress
- `GET /api/tasks/{id}/transcript|summary|media` — fetch artifacts
- `POST /api/tasks/pause|resume|archive|restart_summary` — task control
- `DELETE /api/tasks` — batch delete
- `GET /api/tasks/{id}/subtitles` — the transcript as a WebVTT track
- `GET /api/recordings` — the library of recordings. Each recording carries
  `state`: `processing` (still being produced), `complete` (finished) or
  `incomplete` (processing ended without finishing). A recording that has no
  transcript yet is not listed (nor counted in `total`); it stays reachable by id.
  The list is ordered by `listed_at` (when the recording got its first text),
  newest first. Page with `limit`/`offset`, or with the keyset cursors
  `before_ts`+`before_id` (older) and `after_ts`+`after_id` (newer, with
  `order=asc`), taken from an item's `listed_at` and `id`; each pair must be
  given together. `task_id` (repeatable) keeps only the recordings of those
  tasks. The MCP tool `list_recordings` takes the same parameters
  (`task_ids` for the task filter).
- `GET /api/recordings/{id}` — one recording
- `GET /api/recordings/{id}/text/{variant}` — the recording's text:
  `raw` (transcript), `redacted` (processed transcript) or `summary`, with the
  paginated modes described below
- `GET /api/recordings/{id}/subtitles` — the transcript as a WebVTT track
- `GET /api/recordings/{id}/results/{source}/{ref}` — one prompt result's
  text, as listed in the recording's `prompt_results`

The recording owns its content. The task content routes
(`/api/tasks/{id}/transcript|summary|redacted|subtitles|results/...`) are kept
and answer exactly what the recording routes answer for the task's recording;
a task with no recording answers `404`. Recording content routes keep working
after the task is deleted.
- `GET /api/search` — semantic search across the corpus (see below)
- `GET /api/me` — who am I (acting_as, is_admin)
- `GET /api/admin/users` — admin only

What is **not** exposed:

- Browser auth routes (`/auth/login`, `/auth/callback`, `/auth/logout`)
- API token management (`/api/me/tokens`) — session-only by design, see
  [AUTH.md](AUTH.md#personal-api-tokens) for the rationale
- Server-Sent Events (`/api/events`) — streaming, not modelled in OpenAPI
- Web Push (`/api/push/*`) — browser-PWA specific
- Static assets, the player page, MCP transport

If you want MCP semantics (claude.ai, Claude Desktop, etc.), use the
separate MCP endpoint at `/mcp/` instead — see
[AUTH.md](AUTH.md#mcp-auth-flow).

## Building your own Custom GPT from scratch

A shared GPT link gives the recipient a read-only assistant — they
cannot edit the system prompt or Actions. To give someone full control,
they need to build their own GPT and import the spec. Every VTS user can
do this independently:

1. **Get a personal API token** at `<your-vts>/` → key icon in the
   header → **Create token** → name it (e.g. `chatgpt-laptop`) → copy
   the `vts_…` value. It's only shown once.

2. **Create a GPT.** ChatGPT → *Explore GPTs* → *Create* (requires a
   ChatGPT Plus / Team / Enterprise plan).

3. **Configure → Actions → Create new action → Import from URL.**
   Paste:

   ```
   https://<your-vts>/openapi.json
   ```

   ChatGPT will pull the spec, list every endpoint, and validate it.

4. **Authentication** (still inside the Action editor):
   - Authentication type: **API Key**
   - API Key: paste your `vts_…` token
   - Auth Type: **Bearer**

5. **Privacy policy URL:** ChatGPT requires one to publish the Action.
   Any URL on your domain works (e.g. `<your-vts>/` itself). Nothing
   sensitive about VTS — the API is private to you and your allow-list.

6. **System prompt** (the *Instructions* field at the top): describe
   how you want the assistant to use VTS. A reasonable starter:

   > You help me work with my self-hosted VTS instance. When the user
   > shares a YouTube URL, submit it via `POST /api/tasks` with
   > `transcript=true` and `summary=true`. Poll `GET /api/tasks/{id}`
   > until done, then fetch the summary. When the user asks for a
   > recap of an old task, list with `GET /api/tasks?compact=true&limit=50`
   > (ChatGPT Actions have a ~30KB response budget — `compact=true` and
   > a small `limit` keep responses well under it) and fetch the
   > specific artifact when you've identified the right task.
   >
   > Transcripts can be long. To read a full transcript, paginate:
   > `GET /api/tasks/{id}/transcript?offset=0&limit=25000` returns a
   > JSON `TextSliceOut`. Keep calling with `offset = previous offset +
   > previous length` until `is_end: true`, concatenate the `text`
   > fields. Same pattern works for /summary, /redacted, /log.

7. **Save** the GPT (top-right). It is private to your account by
   default. You can leave it that way; no need to publish.

8. **Test.** Open the new GPT, send "what's my identity?" — the
   first call will prompt for the action to use your token; click
   *Allow always for this site*. Then ask it to do something real
   ("list my last 5 tasks").

What the OpenAPI spec carries is the same for everyone — the endpoints,
the schemas, the auth method. What differs per user is their `vts_…`
token (issued from their own VTS account) and their system prompt.

## Corpus search

`GET /api/search?q=<question>` searches every recording you own and returns the
matching passages — not an answer.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `q` | — | the question, in any language |
| `limit` | 10 | maximum hits |
| `offset` | 0 | skip this many ranked hits (paging) |
| `threshold` | server setting (0.45) | minimum cosine similarity |
| `recording_id` | — | confine the search to one recording |

```json
{
  "query": "what did we decide about pricing",
  "threshold": 0.45,
  "hits": [
    {
      "chunk_id": "...",
      "recording_id": "...",
      "source_task_id": "...",
      "title": "Team sync",
      "text": "...the passage...",
      "start_sec": 5014.5,
      "end_sec": 5061.0,
      "speakers": ["SPEAKER_00"],
      "score": 0.556
    }
  ]
}
```

**An empty `hits` is an answer, not a failure.** It means nothing in your
recordings is relevant enough — the alternative, returning the closest
available text, produces confident nonsense for questions the corpus cannot
answer. The `threshold` comes back with the results so a client can say which
of the two happened. Do not lower it to manufacture matches.

Retrieval works across languages: an English question finds the passage that
answers it in another language.

**Paging stops at the candidate ceiling.** Only a bounded number of candidates
is ranked, so a page starting beyond that comes back empty however many
passages actually qualify. Rather than return that misleading empty page, the
endpoint answers `422` with the ceiling and the advice in the message: narrow
the query (by person, dates or `recording_id`) instead of paging further.

**Following a hit — two routes, different lifetimes.**

| | addressed by | when the media is gone (archived) or the task deleted |
|---|---|---|
| `GET /api/recordings/{id}/transcript?around_sec=` | recording | **works** |
| `/player/{recording_id}?t=` | recording | **shows the transcript** with the cited sentence marked, without media |

Read a passage through the RECORDING: it owns its artifacts, so the transcript
answers whether or not the job that produced it still exists. `around_sec`
returns just the passage around that second — showing one quote in context
should not mean loading a two-hour transcript.

The player link is for a person to WATCH the moment. `/player/{id}` takes a
recording id or a task id. The media belongs to the task, so once it is gone —
typically after the task is archived, which keeps the transcript — the page
shows the transcript alone, with the cited sentence marked. The same holds
when the task has been deleted. `/player/{task_id}?t=` links keep working.

`recording_id` is the identifier to store — it outlives the task.

The same search is available as the MCP tool `search_transcripts`, with the
same threshold and rules. There each hit carries both links ready-made
(`transcript_url`, `player_url`), both addressed by the recording — so a
client never has to assemble one.

Recordings are also addressable as MCP tools: `list_recordings` and
`get_recording_transcript` (with `structured` and `around_sec`). The
task-scoped `get_transcript` remains for asking about a run in progress.

**Interactive view (MCP Apps).** In hosts that implement the MCP Apps
extension (`io.modelcontextprotocol/ui`), `show_recording(recording_id,
at_sec?)` opens the recording in a player view inside the chat: the
transcript with timecodes and speakers, positioned on `at_sec` when given —
pass a search hit's `recording_id` and `start_sec`. The view is the
`ui://vts/player` resource; it loads further pages through
`app_player_blocks`, which is marked `visibility: ["app"]` so hosts keep it
away from the model. In a host without MCP Apps, `show_recording` simply
returns a short text summary; no other tool changed for this.

When `VTS_PUBLIC_BASE_URL` is set and the recording still has its media, the
view also plays it: `app_media_url` (app-only) issues a link signed for that
recording and user, valid for 10 minutes, and the view renews it when it
expires. Clicking a sentence seeks the media, and the playing sentence is
highlighted. The view's CSP allows media from the public origin only. Without
a public base URL the view shows the transcript alone.

`search_transcripts` results open in the `ui://vts/search` view in the same
hosts: the passages found, "show more" while `total` says there are more, and
each passage opening the player on its moment inside the view. The tool's
result itself is unchanged — only its `_meta.ui` is new.

`show_summary(recording_id, ref?)` opens a recording's summary in the
`ui://vts/summary` view, with a switcher to the recording's other finished
prompt results; switching reads them through `get_recording_prompt_result`.
Its "Regenerate…" button re-runs the recording's prompts with a chosen set,
all at once — the same restart as "restart the final summary" in the web UI,
through the `regenerate_summary(recording_id, prompts)` tool. The previous
results are cleared first, so the view asks before running and the tool is
annotated `destructiveHint`. It needs the task that produced the recording;
for a recording whose task was deleted the button is not offered.

`show_speakers(recording_id)` opens the `ui://vts/speakers` view: each voice of
the recording with a short clip to listen to, bound to a person, a new
person, anonymous, or marked noise. Saving goes through the same code as the
web UI's speaker dialog (registry fragment, transcript re-render, re-index);
the view sends plain choices and the server maps them to the same decision
codes the web dialog writes. Clips play through short-lived signed links,
like the player's media. A recording whose task was deleted is shown
read-only.

**Every MCP tool declares whether it reads or writes**, through the standard
`annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) — so a client can group them instead of showing one flat list:

| | tools | meaning |
|---|---|---|
| read-only | 22 | looks, changes nothing |
| writes | 13 | creates or updates; nothing you had is lost |
| destructive | 6 | the five `delete_*` tools and `regenerate_summary` — a client should confirm first |

`openWorldHint` is set on exactly two: `submit_video` (downloads from the open
internet and occupies a GPU for the length of the recording) and
`retry_delivery` (re-sends to an external system). Neither is marked
destructive: a run can be cancelled, and a retry repeats something already
configured.

A test asserts the classification is exhaustive — a tool added without an
annotation fails the build, because a missing hint reads as "unknown" and some
clients treat unknown as safe.

## Tags

Recordings carry tags. A tag belongs to one user; its name is trimmed, inner
whitespace is collapsed to one space, it must be 1–64 characters, and names
are compared case-insensitively ("Work", " work " and "WORK" are one tag; the
first spelling is kept). An invalid name is a `422`.

| Request | Result |
|---------|--------|
| `GET /api/tags` | `[{id, name, count}]` sorted by name; `count` = recordings carrying the tag (unused tags have `0`) |
| `POST /api/tags {"name": "..."}` | `201 {id, name}`; `409 {"detail": "Tag already exists"}` if the name is taken |
| `PATCH /api/tags/{id} {"name": "..."}` | `{id, name}`; `409` if another tag has that name (a change of case alone is allowed); `404` if not yours |
| `DELETE /api/tags/{id}` | `{"unlinked": n}`: the tag is removed from every recording; `404` if not yours |
| `PUT /api/recordings/{id}/tags {"names": [...]}` | Replaces the recording's tags with exactly these names, creating missing ones; returns `[{id, name}]` |
| `PUT /api/tasks/{id}/tags {"names": [...]}` | The same, for the recording of a task |

Task and recording payloads (`GET /api/tasks`, `GET /api/tasks/{id}`,
`GET /api/recordings`, ...) carry `tags: [{id, name}]`, sorted by name.

Filter by tags with the repeatable `tag` query parameter on `GET /api/tasks`,
`GET /api/tasks/count` and `GET /api/recordings`, plus `tag_match=all|any`
(default `all`; any other value is `422`). Names match the way tag names are
compared everywhere (trimmed, case-insensitive). With `all` every given tag
must be on the recording and an unknown name gives an empty result; with `any`
at least one must be, and unknown names are ignored. A task without a
recording has no tags, so a tag filter excludes it. Tag filters combine with
`q`, the date filters and the cursors.

Tags can be set when the task is created: `tags: ["..."]` in the
`POST /api/tasks` body and in `POST /api/uploads/init` (both single-file and
multi-file); a JSON-encoded list in the `tags` form field of
`POST /api/tasks/upload`. The names are validated before anything is created
and applied to the recording created with the task (for chunked uploads, at
`finalize`, which itself takes no tags).

**MCP tools.** `list_tags` returns `[{name, count}]`; `tag_recording(recording_id,
add=[], remove=[])` adds and removes tags (creating missing ones) and returns the
recording's tag names after the change. `submit_video` takes `tags` (names,
validated before anything is created), `list_tasks` and `list_recordings` take
`tags` and `tag_match` like the HTTP filters, and their items carry `tags` as a
list of names sorted by name. The MCP preset tools filter tag ids exactly as the
HTTP routes do.

**Delivery adapters** receive the recording's tags as `payload.task.tags`, a
tuple of names sorted by name (contract 1.4, additive: an adapter declaring an
older minor still loads, and it is empty when the recording is untagged).

**Presets on submit.** `POST /api/tasks` takes `preset: {source: "system"|"user", id}`.
The preset fills every option the body does not send (`language`, `audio_only`,
`transcript`, `diarize`, `prompts`, `delivery`, and for a user preset `tags`);
an option sent in the body wins, even when its value is the default. The merged
options are validated again, so a preset that turns the transcript off and a body
that asks for prompts is a `422`. An unknown preset is a `404`. Delivery taken from
a preset is not checked against loaded adapters at submit time: a missing plugin
parks the delivery instead of failing the task. The server-side MCP `submit_video`
takes the same `preset`, but there "sent" means "not the default value".

User presets store tag **ids** in `options.tags`, so renaming a tag does not
break a preset. Ids that are not one of your existing tags are dropped when
the preset is saved and skipped when it is read; system presets always have
`tags: []`.

## Paginated reads of large text artifacts

`/api/tasks/{id}/transcript`, `/summary`, `/redacted`, `/log` and
`/api/recordings/{id}/text/{variant}` can
exceed the response budget of constrained clients (ChatGPT Custom
Actions cap at ~30KB). Three modes are available:

1. **Default (UI / browsers):** plain text, full body. Unchanged.
2. **JSON pagination:** `Accept: application/json` (or any `?offset=` /
   `?limit=` query) returns a `TextSliceOut`:
   ```json
   {"text": "...", "offset": 0, "length": 25000, "total_length": 87234, "is_end": false}
   ```
   Call repeatedly with `offset += length` until `is_end: true`. Hard
   safety cap of 200000 chars per slice.
3. **HTTP Range** (`Range: bytes=0-24999`) returns `206 Partial Content`
   with `Content-Range: bytes 0-24999/87234`. Standard HTTP — good for
   curl/wget/programmatic clients. Suffix-range (`bytes=-N`) is not
   supported; compute the offset from `total_length` instead.

ChatGPT Custom Actions support modes 1 and 2 but not 3 — the Action
infrastructure doesn't forward arbitrary request headers to your server.
For GPT, use `?offset=&limit=` (the Action spec includes them as query
parameters, GPT picks them up automatically).

## Chunked uploads

Files above a server-configured threshold (the web UI checks
`GET /api/uploads/config`) go through a small chunked protocol under
`/api/uploads` instead of the single-shot `POST /api/tasks/upload`:
`POST /api/uploads/init` starts a session, `PATCH /api/uploads/{id}?offset=N`
appends a chunk, `GET /api/uploads/{id}/offset` reports how much has been
received (for resuming after a dropped connection), and
`POST /api/uploads/{id}/finalize` turns the assembled file into a task.

### Multi-file sets

`POST /api/uploads/init` accepts a `files` array instead of a single
`filename`/`total_size`:

```json
{"files": [{"filename": "part1.m4a", "total_size": 1048576, "last_modified": 1785000000000}]}
```

The response carries `files: [{index, filename}]`; chunks are then sent with
`PATCH /api/uploads/{id}?offset=N&index=K` (`index` defaults to `0`, which is
what keeps the single-file client working unchanged), and resumed with
`GET /api/uploads/{id}/offset?index=K`. `finalize` returns one task.

A set must be all-video or all-audio (mixed sets are rejected at `init`), the
combined size must not exceed `max_upload_bytes` (2 GiB by default), and it
may contain at most `upload_max_files` files (10 by default). Both limits are
checked at `init`, before any bytes are transferred. Video parts must share
codec, resolution, frame rate, audio codec, sample rate and channel count —
mismatched sets are rejected at `finalize`, because stream-copy concat of
mismatched inputs silently produces a wrong-duration file rather than an
error.

## Curl quick reference

```bash
TOKEN=vts_...
BASE=https://vts.example.com

# What's my identity?
curl -H "Authorization: Bearer $TOKEN" $BASE/api/me

# Submit a YouTube URL
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"source_url":"https://youtu.be/XYZ","options":{"transcript":true,"summary":true}}' \
     $BASE/api/tasks

# Upload a local recording
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@recording.mp4" \
     -F "transcript=true" -F "summary=true" \
     $BASE/api/tasks/upload

# Poll a task
curl -H "Authorization: Bearer $TOKEN" $BASE/api/tasks/<task_id>

# Fetch the markdown summary once it's ready
curl -H "Authorization: Bearer $TOKEN" $BASE/api/tasks/<task_id>/summary
```

For the OBS Studio integration (auto-upload on recording stop), see
[scripts/obs/README.md](../scripts/obs/README.md).
