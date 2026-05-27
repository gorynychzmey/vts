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
