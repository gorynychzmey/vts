# MCP OAuth (Google) — Design

**Date:** 2026-05-24
**Status:** Draft, awaiting user review
**Beads:** vts-8l1
**Prereq:** [2026-05-22-mcp-server-design.md](2026-05-22-mcp-server-design.md) shipped

## Goal

Let `claude.ai` and `chatgpt.com` (and any other MCP client that only
supports OAuth) connect to `https://vts.vostrikov.dev/mcp/`. Both
products require **MCP-spec OAuth 2.1 with Dynamic Client Registration**;
bearer tokens, header-auth, and basic-auth are not options.

Identity is derived from the user's Google account: `email` becomes the
vts `username`. The existing reverse-proxy OIDC integration (web UI
auth) is unchanged — this design adds a parallel auth path that lives
entirely inside vts, scoped to the `/mcp/*` subtree.

## Non-goals

- Multi-tenant per-user OAuth client_id: one Google OAuth client serves
  the whole vts instance.
- Custom consent screen: FastMCP's built-in `/mcp/consent` is fine.
- Token revocation API in vts: FastMCP rotates refresh tokens internally.
- A web UI to manage MCP integrations: out of scope.

## Architecture

```
                  ┌─────────────────────────────────────┐
   claude.ai  ──▶│  Cloudflare → traefik              │
   chatgpt.com    │   /mcp/*  → vts (no OIDC middleware)│
                  │   /...    → vts (OIDC middleware)   │
                  └─────────────────────────────────────┘
                            │
                            ▼
                  ┌─────────────────────────────────────┐
                  │  vts webapi                         │
                  │   /mcp/                             │
                  │     ├─ .well-known/oauth-auth-srv  │ ← FastMCP GoogleProvider
                  │     ├─ .well-known/oauth-prot-res  │   exposes all of these
                  │     ├─ register   (DCR)            │
                  │     ├─ authorize                   │
                  │     ├─ token                       │
                  │     ├─ auth/callback               │   ← Google redirects here
                  │     ├─ consent                     │
                  │     └─ /          (JSON-RPC tools) │
                  └─────────────────────────────────────┘
                            │  (validates Google id_token)
                            ▼
                       Google OAuth
```

Three things move from infra into vts:

1. **Traefik:** stop applying OIDC middleware to anything under
   `/mcp/*`. A separate router with `priority: 100` is enough.
2. **vts/mcp/server.py:** pass `auth=GoogleProvider(...)` to
   `FastMCP(...)` when `mcp_oauth_enabled`.
3. **vts/mcp/auth.py:** when OAuth is enabled, resolve the user from
   `fastmcp.server.dependencies.get_access_token()` instead of
   `X-Forwarded-User`.

The web UI path (`/api/*`, `/`, `/static/*`) is untouched.

## Routes and URLs (with `app.mount("/mcp", mcp_app)`)

FastMCP `GoogleProvider` publishes routes relative to the sub-app root.
With our mount at `/mcp` they become:

| External URL | Purpose |
|---|---|
| `https://vts.vostrikov.dev/mcp/.well-known/oauth-authorization-server` | RFC 8414 metadata |
| `https://vts.vostrikov.dev/mcp/.well-known/oauth-protected-resource` | RFC 9728 metadata |
| `https://vts.vostrikov.dev/mcp/register` | Dynamic Client Registration (RFC 7591) |
| `https://vts.vostrikov.dev/mcp/authorize` | OAuth authorize |
| `https://vts.vostrikov.dev/mcp/token` | OAuth token |
| `https://vts.vostrikov.dev/mcp/auth/callback` | Google → vts redirect |
| `https://vts.vostrikov.dev/mcp/consent` | FastMCP default consent screen |
| `https://vts.vostrikov.dev/mcp/` | MCP JSON-RPC endpoint |

**This is achieved by setting `base_url="https://vts.vostrikov.dev/mcp"`
and `redirect_path="/auth/callback"`** on the `GoogleProvider`. The
`base_url` includes the `/mcp` prefix, so all URLs in published
metadata correctly point under `/mcp/...`. The `redirect_path` is
relative to the sub-app root, so it becomes `/mcp/auth/callback`
externally.

Verified by the controller via in-process ASGI smoke test against the
installed FastMCP 3.3.1: the metadata document advertises
`https://example.test/mcp/authorize` etc., matching the expectation.

## Identity flow

1. Client (claude.ai) hits `/mcp/` without a token → vts responds
   `401 + WWW-Authenticate` pointing at the metadata URL.
2. Client discovers the metadata, registers via DCR (`/mcp/register`),
   initiates OAuth (`/mcp/authorize`), gets redirected to Google.
3. User logs in to Google, consents.
4. Google redirects back to `/mcp/auth/callback` with an authorization
   code. FastMCP exchanges the code for Google tokens, fetches Google
   `userinfo` (email + profile).
5. FastMCP issues its **own** access token (JWT, signed) to the client,
   embedding the Google email as a claim.
6. Subsequent client requests carry `Authorization: Bearer <vts-token>`.
   FastMCP validates the JWT, populates `AccessToken` context for the
   request.
7. Each MCP tool calls `mcp_authenticate(session)`. New flow:
   a. Read `AccessToken` via `get_access_token()`.
   b. Extract `email` from claims (Google sets this from userinfo).
   c. Lowercase + strip.
   d. Allow-list check: `email in allowed_emails OR
      email.split('@')[1] in allowed_domains`. If neither matches → 403.
   e. `repo.get_or_create_user(email)` → AuthenticatedUser.

If `mcp_oauth_enabled=false`, the old `X-Forwarded-User` path runs
verbatim (development and legacy parity).

## Allow-list semantics (OR logic)

The user is permitted if **at least one** of these holds:

- email is in `mcp_oauth_allowed_emails` (case-insensitive, exact match), OR
- email's domain is in `mcp_oauth_allowed_domains` (case-insensitive).

Both lists empty when OAuth is enabled → every authenticated user gets
403. (Fail-safe: discovering the OAuth flow does not implicitly grant
access.)

## Configuration

Both `.env` and `config.yaml` are supported via the existing loader.
YAML overrides env (this is the established repo convention; see
`vts/core/config.py:236-239`). Keep secrets out of YAML — set
`client_secret` only in env so the YAML can be committed-safe.

### New Settings fields

```python
class Settings(BaseSettings):
    ...
    mcp_oauth_enabled: bool = False
    mcp_oauth_client_id: str | None = None
    mcp_oauth_client_secret: str | None = None
    mcp_oauth_base_url: str | None = None
    mcp_oauth_allowed_emails: list[str] = []
    mcp_oauth_allowed_domains: list[str] = []

    @field_validator(
        "mcp_oauth_allowed_emails",
        "mcp_oauth_allowed_domains",
        mode="before",
    )
    @classmethod
    def _csv_or_json_list(cls, v):
        """Accept comma-separated string for friendly env input;
        JSON arrays and YAML lists pass through unchanged."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return v  # pydantic-settings parses JSON
            return [item.strip() for item in v.split(",") if item.strip()]
        return v
```

### env example (`.env` / systemd EnvironmentFile)

```bash
VTS_MCP_OAUTH_ENABLED=true
VTS_MCP_OAUTH_CLIENT_ID=123456789-abcdef.apps.googleusercontent.com
VTS_MCP_OAUTH_CLIENT_SECRET=GOCSPX-...
VTS_MCP_OAUTH_BASE_URL=https://vts.vostrikov.dev/mcp
VTS_MCP_OAUTH_ALLOWED_DOMAINS=vostrikov.de,vostrikov.dev
# (optional) extra individual addresses
VTS_MCP_OAUTH_ALLOWED_EMAILS=guest@gmail.com
```

### yaml example (`/opt/vts/config/config.yaml`)

```yaml
mcp:
  enabled: true            # existing flag, kept here for completeness
  path: /mcp               # existing flag
  oauth:
    enabled: true
    client_id: 123456789-abcdef.apps.googleusercontent.com
    # client_secret intentionally omitted — set only in env-file:
    #   /opt/vts/config/vts.env  →  VTS_MCP_OAUTH_CLIENT_SECRET=...
    base_url: https://vts.vostrikov.dev/mcp
    allowed_domains:
      - vostrikov.de
      - vostrikov.dev
    allowed_emails: []
```

Naming nuance: nested YAML keys auto-flatten with underscore, so
`mcp.oauth.client_id` becomes the Settings field `mcp_oauth_client_id`.
This is the convention the rest of `config.yaml` already follows.

## Code changes

- **`vts/core/config.py`**: six new fields + the validator above.
- **`vts/mcp/server.py:build_mcp_server`**: branch on
  `settings.mcp_oauth_enabled`. If true, construct `GoogleProvider`:
  ```python
  from fastmcp.server.auth.providers.google import GoogleProvider
  auth = GoogleProvider(
      client_id=settings.mcp_oauth_client_id,
      client_secret=settings.mcp_oauth_client_secret,
      base_url=settings.mcp_oauth_base_url,
      redirect_path="/auth/callback",
      required_scopes=["openid", "email"],
  )
  mcp = FastMCP(name="vts", auth=auth)
  ```
  Otherwise, `mcp = FastMCP(name="vts")` (existing path).
- **`vts/mcp/auth.py`**: `mcp_authenticate(session)` learns two modes.
  Drop the `http_request` parameter — neither mode needs it. The
  OAuth-enabled path uses `get_access_token()` and runs the allow-list
  check; raises 401 if no token, 403 if email not allowed. The disabled
  path falls back to `get_http_request()` + `X-Forwarded-User`.
- **`vts/mcp/server.py`**: 6 tool wrappers' calls to
  `mcp_authenticate(get_http_request(), session)` simplify to
  `mcp_authenticate(session)`.
- **`vts/services/auth.py:resolve_user_from_request`**: untouched.
  REST path stays the same.
- **`.env.example`** and `README.md`: new section "MCP OAuth (for
  claude.ai / ChatGPT)" with the GCP Console steps and env vars.
- **`docs/INITIAL_DEPLOYMENT.md`** (if present): note the Traefik
  bypass router and the Google OAuth client setup.

## Operator-side changes (not code)

### Google Cloud Console

Create a new OAuth 2.0 Client ID (separate from any existing
auth.vostrikov.dev client):

- **Application type:** Web application
- **Authorized JavaScript origins:** (leave empty)
- **Authorized redirect URIs:**
  `https://vts.vostrikov.dev/mcp/auth/callback`
- **Scopes:** `openid`, `email`

Take `client_id` and `client_secret` to the vts env-file.

### Traefik

Add a higher-priority router that skips the OIDC middleware:

```yaml
http:
  routers:
    vts-mcp:
      rule: "Host(`vts.vostrikov.dev`) && PathPrefix(`/mcp`)"
      service: vts
      priority: 100
      # No middlewares: vts handles OAuth internally for /mcp/*.
```

The existing `vts` router (with OIDC middleware, default priority)
keeps handling everything else.

## Testing

- **Unit:**
  - `mcp_oauth_allowed_emails`/`mcp_oauth_allowed_domains` parsing
    accepts JSON, CSV string, plain list.
  - Allow-list helper: empty/empty → reject; email-only match; domain
    match; case-insensitivity; empty email reject.
  - `mcp_authenticate` OAuth branch: mock `get_access_token` to return
    a token with various claims; assert pass/fail.
  - `mcp_authenticate` legacy branch: untouched test stays green.
- **Integration:**
  - Existing FastMCP in-process `Client(mcp)` test continues to work
    (without OAuth — flag off).
- **Manual e2e (not automated):**
  - Connect from `claude.ai → Settings → Connectors → Custom`.
    URL `https://vts.vostrikov.dev/mcp/`. Verify OAuth flow, tool list,
    a real `list_tasks` call.

## Open implementation questions

1. **`get_access_token()` claim shape.** `GoogleProvider` is documented
   to put Google's `email` into the access token's claims, but the
   exact field name (`email` vs `sub_email` vs `claims.email`) must be
   confirmed at coding time by reading
   `.venv/lib/python3.14/site-packages/fastmcp/server/auth/providers/google.py`.
2. **`require_authorization_consent` default.** FastMCP defaults to
   `True` (consent screen on every authorize). For a personal vts this
   is friction; consider `"remember"` so the user consents once per
   browser. Confirm UX before flipping.
