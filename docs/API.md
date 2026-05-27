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

What gets exposed (everything under `/api/tasks`, `/api/me`,
`/api/admin`, `/api/version`):

- `POST /api/tasks` — submit by URL
- `POST /api/tasks/upload` — upload a file (multipart)
- `GET /api/tasks` — list
- `GET /api/tasks/{id}` — status / progress
- `GET /api/tasks/{id}/transcript|summary|media` — fetch artifacts
- `POST /api/tasks/pause|resume|archive|restart_summary` — task control
- `DELETE /api/tasks` — batch delete
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
   > recap of an old task, search via `GET /api/tasks` and fetch the
   > relevant artifact.

7. **Save** the GPT (top-right). It is private to your account by
   default. You can leave it that way; no need to publish.

8. **Test.** Open the new GPT, send "what's my identity?" — the
   first call will prompt for the action to use your token; click
   *Allow always for this site*. Then ask it to do something real
   ("list my last 5 tasks").

What the OpenAPI spec carries is the same for everyone — the endpoints,
the schemas, the auth method. What differs per user is their `vts_…`
token (issued from their own VTS account) and their system prompt.

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
