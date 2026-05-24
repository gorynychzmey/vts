# Web UI OAuth (cookie session) + unified OAuth — Design

**Date:** 2026-05-24
**Status:** Draft, awaiting user review
**Beads:** vts-u8n
**Builds on:** [2026-05-24-mcp-oauth-design.md](2026-05-24-mcp-oauth-design.md)

## Goal

Make vts a self-contained OAuth-authenticated web app. Drop the Traefik
forward-auth OIDC middleware entirely; vts handles login itself for both
the browser (PWA / web UI) and MCP clients. After this lands:

- Traefik routes `Host(vts.vostrikov.dev)` straight to vts with **no
  middleware** and no path-prefix bypasses.
- A browser visiting `/` is redirected to Google, logs in, gets a
  `vts_session` cookie, and uses the app.
- An MCP client (claude.ai, ChatGPT, Claude Desktop) connects to
  `https://vts.vostrikov.dev/mcp/`, the FastMCP GoogleProvider handles
  its OAuth dance and issues a Bearer access token.
- Both paths share one Google OAuth client (one `client_id`, two
  redirect URIs).
- The `X-Forwarded-User` header is no longer consulted in production.

## Non-goals

- Pluggable OAuth providers (Google → GitHub / Authentik / generic OIDC).
  That work is tracked separately as `vts-tlw` and will reshape the
  config introduced here. This spec uses Google directly via authlib but
  isolates the upstream-specific bits so vts-tlw is a swap, not a rewrite.
- Refresh-token rotation / silent re-auth. The session cookie lives 30
  days; expiry forces a fresh OAuth round-trip.
- Server-side session store. Cookies are signed and self-contained.
- 2FA, password reset, account linking, audit logs.
- Per-user OAuth client_ids.

## Architecture

```
                    ┌──────────────────────────────┐
   Browser (PWA)    │  Cloudflare → Traefik         │
   claude.ai        │   Host(vts.vostrikov.dev)    │ ← single router,
   chatgpt          │   NO middlewares             │   no OIDC, no bypasses
                    └──────────────────────────────┘
                              │
                              ▼
                    ┌──────────────────────────────┐
                    │  vts webapi                  │
                    │                              │
                    │   Routes:                    │
                    │     /             — UI       │
                    │     /api/*        — REST     │
                    │     /auth/login              │ ← new
                    │     /auth/callback           │ ← new
                    │     /auth/logout             │ ← new
                    │     /mcp/*        — FastMCP  │
                    │                              │
                    │   Auth resolution (per req): │
                    │     vts_session cookie       │ ← browser
                    │     Authorization: Bearer …  │ ← MCP (FastMCP)
                    │     VTS_OAUTH_ENABLED=false  │ ← dev only:
                    │       → X-Forwarded-User     │   no Google needed
                    └──────────────────────────────┘
                              │
                              ▼
                       Google OAuth (one client_id)
```

### What disappears

- Traefik `forward-auth` middleware in front of vts.
- All `PathPrefix(/mcp)` / `PathPrefix(/.well-known/oauth-*)` bypass
  rules in Traefik.
- `Settings.trusted_proxy_cidrs` and `Settings.is_trusted_proxy(...)`.
- Reading `X-Forwarded-User` in production. (Kept only as the dev-mode
  bypass when `oauth_enabled=False`.)
- The `Header(alias="X-Forwarded-User")` parameter on the FastAPI
  dependency.

### What appears

- `vts/services/web_oauth.py` — authlib client wrapping Google.
- `vts/services/session.py` — sign / verify the `vts_session` cookie
  using `itsdangerous.TimestampSigner`.
- `vts/api/auth_routes.py` — `/auth/login`, `/auth/callback`,
  `/auth/logout` Starlette routes.
- A request-level helper (`require_browser_user`) used as a
  `Depends(...)` on the existing REST routes and as a redirect-or-deny
  hook for the HTML index route.

### What changes shape

- `Settings` collapses `mcp_oauth_*` → `oauth_*` for the shared bits
  (client_id, client_secret, allowed_emails, allowed_domains).
  `mcp_oauth_base_url` is **removed**; a single `public_base_url`
  (host-only, e.g. `https://vts.vostrikov.dev`) replaces it. MCP's
  GoogleProvider base_url is computed as
  `public_base_url + mcp_path`. Old env names accept the new value
  as a deprecation alias.
- `vts/services/auth.py:resolve_user_from_request` is rewritten to
  follow the cookie / bearer / dev-mode path described below.
- `vts/static/app.js` stops sending an `X-Forwarded-User` header in
  `fetch()`; instead it relies on the cookie. On 401 from `/api/me` it
  redirects to `/auth/login?next=…`.

## Authentication algorithm (single source of truth)

`resolve_user_from_request(request, session, settings)` is the only
auth entry point. New algorithm:

```
1. If settings.oauth_enabled is False:
     # Local dev — trust X-Forwarded-User as before.
     email = request.headers["X-Forwarded-User"]
     if not email: raise 401
     return user_for(email)

2. If request has Authorization: Bearer …:
     # FastMCP / MCP path.
     token = get_access_token()  # validates the token, fetches claims
     if token is None: raise 401
     email = token.claims["email"]
     if not is_email_allowed(email, ...): raise 403
     return user_for(email)

3. If request has cookie 'vts_session':
     # Browser path.
     payload = session_signer.unsign(cookie, max_age=30 days)
     if invalid / expired: raise 401
     email = payload["email"]
     # Allow-list was checked at login time; we trust the cookie.
     return user_for(email)

4. raise 401
```

`user_for(email)` is the existing repo lookup-or-create that yields
`AuthenticatedUser`. `as_user` admin-switch logic stays on top of step
2 / step 3 (only when `is_admin(email)` is true).

The HTML index route (`GET /`) catches `HTTPException(401)` from the
resolver and converts it to `302 -> /auth/login?next=/`. All
`/api/*` and `/mcp/*` routes let the 401 propagate; clients handle it.

## Session cookie design

- **Name:** `vts_session`.
- **Format:** `itsdangerous.TimestampSigner` over a base64-encoded JSON
  payload `{"email": "alice@example.com"}`. The signer adds a timestamp
  for max-age checks; we don't need a separate `iat`/`exp`.
- **Attributes:** `HttpOnly; Secure; SameSite=Lax; Path=/`. No
  `Domain` (host-only cookie). `Max-Age=2592000` (30 days). No
  auto-refresh on activity — the cookie's signed timestamp drives
  expiry.
- **Signing key:** `Settings.session_secret`. If set, use as-is. If
  unset, derive deterministically from `oauth_client_secret`:
  ```python
  derive_jwt_key(low_entropy_material=oauth_client_secret,
                 salt="vts-session-cookie-signing-key")
  ```
  (`fastmcp.utilities` already provides `derive_jwt_key` — reuse it.)
- **Rotation:** changing `session_secret` (or `oauth_client_secret`)
  invalidates all sessions. Documented behaviour, no migration path.

## OAuth flow for the browser

Library: **authlib** (`authlib.integrations.starlette_client.OAuth`).

### `GET /auth/login?next=<path>`

1. Build authlib `OAuth` client lazily on first request (or in app
   lifespan).
2. Validate `next`: must start with `/`, must not start with `//`, must
   not contain `\\` or scheme. If invalid → `next = "/"`.
3. Generate PKCE pair via authlib (`code_verifier`, `state`).
4. Set a short-lived signed cookie `vts_oauth_state` containing
   `{"verifier": …, "next": …}`, `Max-Age=300`, `HttpOnly Secure
   SameSite=Lax Path=/auth/callback`.
5. Return `302` to Google's `accounts.google.com/o/oauth2/v2/auth?…`
   with `redirect_uri=https://<host>/auth/callback`, `scope=openid
   email`, `state=<state>`, `code_challenge=<challenge>`,
   `code_challenge_method=S256`.

### `GET /auth/callback?code=…&state=…`

1. Read and verify `vts_oauth_state` cookie; bail with 400 on mismatch
   or expiry.
2. Exchange `code` for tokens via authlib (handles PKCE).
3. Decode and verify the `id_token` (authlib does signature + audience
   + expiry).
4. Extract `email` claim. If absent → 400.
5. Pass through `is_email_allowed(email, oauth_allowed_emails,
   oauth_allowed_domains)`. If not allowed → 403 HTML page ("not
   authorised, sign out of Google or use another account").
6. `repo.get_or_create_user(email)`.
7. Sign and set `vts_session` cookie. Delete `vts_oauth_state`.
8. `302 → next` (validated path from step 1).

### `POST /auth/logout`

1. Set `vts_session` to empty value with `Max-Age=0`.
2. Return `204`. Frontend reloads `/`.

(There is no Google-side logout — we only forget our own cookie. If
the user wants to fully sign out, they remove the app's access in
their Google account.)

## MCP side — what changes

Functionally nothing. The existing FastMCP `GoogleProvider` setup from
the previous spec stays exactly as is. The only edits:

- `GoogleProvider(client_id=..., client_secret=...)` reads the shared
  `settings.oauth_client_id` / `settings.oauth_client_secret` instead
  of the old `mcp_oauth_*`.
- `base_url = f"{settings.public_base_url}{settings.mcp_path}"`
  (composed at server-construction time; no separate setting).
- `is_email_allowed(..., settings.oauth_allowed_*)` (shared lists).
- `mcp_authenticate` keeps two branches (legacy / OAuth) but the legacy
  branch now equals "no auth at all when `oauth_enabled=False`" — no
  `X-Forwarded-User` reading. In dev with `oauth_enabled=False`,
  MCP tools effectively run with whatever fallback identity the dev
  test wires up. Document this; do not surprise.

## Configuration

All settings consolidated under `oauth_*`. New canonical names:

```python
class Settings(BaseSettings):
    public_base_url: str | None = None       # e.g. "https://vts.vostrikov.dev"
    oauth_enabled: bool = False
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_allowed_emails: list[str] = []
    oauth_allowed_domains: list[str] = []
    session_secret: str | None = None
```

`public_base_url` is **host-only** (no path). All URL derivations
compute paths from it:

- Web OAuth `redirect_uri` = `public_base_url + "/auth/callback"`.
- MCP `GoogleProvider(base_url=...)` = `public_base_url + mcp_path`
  (e.g. `https://vts.vostrikov.dev/mcp`).
- Future endpoints that need self-reference (e.g. Web Push WebPush
  audience) reuse the same value.

`mcp_oauth_base_url` is **removed** from the config in favour of this
single source. If the migration aliases (below) see the old key, they
warn AND fall back to splitting it: `mcp_oauth_base_url=
"https://vts.vostrikov.dev/mcp"` becomes
`public_base_url="https://vts.vostrikov.dev"` plus the already-existing
`mcp_path="/mcp"`. This keeps existing deployments working through the
deprecation window.

### Deprecated aliases

Until at least 1.2.x, the old `mcp_oauth_*` env keys are still accepted
and copied into the canonical `oauth_*` ones at Settings construction
time, with a `DeprecationWarning` printed at startup. The migration
table:

| Deprecated env | Canonical env |
|---|---|
| `VTS_MCP_OAUTH_ENABLED` | `VTS_OAUTH_ENABLED` |
| `VTS_MCP_OAUTH_CLIENT_ID` | `VTS_OAUTH_CLIENT_ID` |
| `VTS_MCP_OAUTH_CLIENT_SECRET` | `VTS_OAUTH_CLIENT_SECRET` |
| `VTS_MCP_OAUTH_ALLOWED_EMAILS` | `VTS_OAUTH_ALLOWED_EMAILS` |
| `VTS_MCP_OAUTH_ALLOWED_DOMAINS` | `VTS_OAUTH_ALLOWED_DOMAINS` |
| `VTS_MCP_OAUTH_BASE_URL` | `VTS_PUBLIC_BASE_URL` (with `mcp_path` stripped if present) |

`Settings.trusted_proxy_cidrs` and `Settings.is_trusted_proxy(...)` are
deleted outright (no alias). Existing configs that set them get
`extra="ignore"` behaviour — settings are silently dropped. Documented.

### `.env` example (new section)

```bash
# Auth — Google OAuth for both web UI and MCP.
VTS_OAUTH_ENABLED=true
VTS_OAUTH_CLIENT_ID=...apps.googleusercontent.com
VTS_OAUTH_CLIENT_SECRET=GOCSPX-...
VTS_OAUTH_ALLOWED_DOMAINS=vostrikov.de,vostrikov.dev
# (optional) extra individuals not covered by the domain list:
VTS_OAUTH_ALLOWED_EMAILS=

# Cookie signing. If unset, derives from VTS_OAUTH_CLIENT_SECRET — fine
# for personal use; set explicitly when running multiple replicas or
# rotating the OAuth client secret without invalidating sessions.
VTS_SESSION_SECRET=

# Public origin where vts is reachable from the outside. No path.
# Used to build OAuth redirect URIs (both web and MCP) and any other
# self-referential URL the service needs to advertise.
VTS_PUBLIC_BASE_URL=https://vts.vostrikov.dev
```

### `config.yaml` equivalent

```yaml
public_base_url: https://vts.vostrikov.dev
oauth:
  enabled: true
  client_id: ...apps.googleusercontent.com
  # client_secret in env only
  allowed_domains: [vostrikov.de, vostrikov.dev]
  allowed_emails: []
# session_secret in env only
mcp:
  path: /mcp     # already existed; combined with public_base_url
                 # gives the MCP GoogleProvider its base_url.
```

## Frontend changes

Minimal. Single file: `vts/static/app.js`.

1. `api(path, options)` — drop the `headers["X-Forwarded-User"] =
   state.authUser` line. `fetch()` already sends the same-origin cookie
   by default; no `credentials: 'include'` needed (same origin).
2. On bootstrap, fetch `/api/me`. On `401` →
   `window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname + window.location.search)`.
3. Add a "Logout" affordance somewhere unobtrusive (top right of the
   header). Click → `POST /auth/logout` → reload `/`.

Service worker, share_target, push subscribe — no changes needed. They
all run on the same origin and the cookie is sent automatically.

PWA install does NOT cache `/auth/*`. Confirmed by reading `sw.js` (it
already excludes non-API paths from runtime caching).

## Operator-side changes

### GCP Console

Existing OAuth client gains a second redirect URI:

- `https://vts.vostrikov.dev/auth/callback` (new — web UI)
- `https://vts.vostrikov.dev/mcp/auth/callback` (existing — MCP)

Same `client_id` / `client_secret`; no other Google-side edits.

### Traefik

Strip the OIDC middleware and the MCP bypass rules. Final state is a
single router:

```yaml
http:
  routers:
    vts:
      rule: "Host(`vts.vostrikov.dev`)"
      service: vts
      # no middlewares
      observability:
        accessLogs: false
```

## Testing

### Unit
- `vts/services/session.py` — sign / unsign round-trip; expired
  cookie raises; tampered cookie raises; wrong key raises.
- `vts/services/web_oauth.py` — login-redirect builds the correct URL;
  callback rejects state mismatch; callback rejects disallowed email;
  callback issues a session cookie on the happy path. authlib calls to
  Google are mocked.
- `resolve_user_from_request` — three explicit tests:
  - oauth_enabled=False + X-Forwarded-User present → user.
  - oauth_enabled=True + valid cookie → user.
  - oauth_enabled=True + valid Bearer (mocked) → user.
  - oauth_enabled=True + nothing → 401.
- `Settings` — alias mapping: `VTS_MCP_OAUTH_CLIENT_ID=foo` (no
  canonical key set) → `settings.oauth_client_id == "foo"`. Both keys
  set → canonical wins, deprecation warning fired.

### Integration
- One pytest using `httpx.AsyncClient(transport=ASGITransport(...))`
  against the FastAPI app: open `/`, expect `302 → /auth/login`;
  follow `/auth/login`, expect `302 → accounts.google.com/...`.
- One pytest that monkeypatches authlib's token-exchange to return a
  fake Google id_token claim set, calls `/auth/callback`, expects
  `302 → /` with a `Set-Cookie: vts_session=...` header.

### Manual e2e (not automated)
- Wipe Claude Desktop's vts connector, reconnect via OAuth, run
  `list_tasks` → success.
- Open `https://vts.vostrikov.dev/` in a fresh incognito browser,
  expect redirect to Google login, then land on `/` with the task
  list.
- Visit `https://vts.vostrikov.dev/` already logged in → no redirect.

## Migration / rollout

This is a coordinated change: code + Google Console + Traefik must
land together. Recommended sequence:

1. Add the second redirect URI in GCP Console (additive, harmless).
2. Deploy vts with the new code AND `VTS_OAUTH_ENABLED=true`.
3. Strip the OIDC middleware in Traefik dynamic config.
4. Verify browser flow and MCP flow.

If browser flow breaks: revert Traefik step (re-enable OIDC) — vts
still works because `oauth_enabled` controls the new flow
independently.

If vts itself fails to start: revert vts version. Traefik OIDC keeps
the old auth path alive against the old build.

## Code structure

```
vts/
  core/
    config.py            — add oauth_* fields, session_secret;
                           drop trusted_proxy_cidrs / is_trusted_proxy;
                           add MCP→canonical deprecation aliases
  services/
    auth.py              — rewrite resolve_user_from_request
    session.py           — NEW: cookie sign / unsign
    web_oauth.py         — NEW: authlib client wrapper
  api/
    auth_routes.py       — NEW: /auth/login, /auth/callback, /auth/logout
    main.py              — register auth_routes; HTML 401-on-/ → redirect
  mcp/
    server.py            — read new oauth_* settings
    auth.py              — drop dev-mode X-Forwarded-User branch
                           (resolve_user_from_request handles it)
  static/
    app.js               — drop X-Forwarded-User header; redirect on 401;
                           add Logout
```

Plus `requirements.txt`: add `authlib>=1.6`, `itsdangerous>=2.2` (the
latter is already a Starlette dependency but make it explicit).

Plus README + `.env.example`: document the new flow.

## Open implementation questions

These need to be checked at coding time, not before.

1. **authlib + Starlette integration:** confirm
   `authlib.integrations.starlette_client.OAuth` works inside FastAPI
   (it should — FastAPI is Starlette). If friction, fall back to
   the lower-level `authlib.oauth2.rfc6749.clients.OAuth2Client` and
   build the redirect URL manually.

2. **`id_token` verification:** authlib's `parse_id_token` needs the
   client's JWKs to be loadable. Confirm `oauth.create_client("google")`
   discovers `server_metadata_url=
   "https://accounts.google.com/.well-known/openid-configuration"` —
   then id_token verification is automatic.

3. **`session_secret` derivation:** `derive_jwt_key` lives in
   `fastmcp.utilities.auth`. Confirm it's a stable public API on
   FastMCP 3.3.1 (used by `OAuthProxy` already; should be).
   Alternative: `hashlib.blake2b(client_secret, salt=...).digest()`.

4. **`SessionMiddleware`** — authlib's Starlette helper expects
   Starlette's session middleware. We don't need it (we manage our
   own `vts_session` cookie + a separate `vts_oauth_state` cookie),
   but during OAuth dance authlib may insist. If so, add Starlette's
   `SessionMiddleware` (signed-cookie based) with `session_secret`,
   scoped to `/auth/*` only via a path check, or accept that authlib
   stores its temp state in Starlette's session cookie.

5. **Cookie flag interactions on the dev host (no TLS):** `Secure`
   blocks the cookie on `http://localhost`. For dev (`oauth_enabled=
   False` anyway), we skip the cookie entirely. If someone tries
   OAuth on a localhost setup over HTTP, the cookie won't stick. Out
   of scope — document it.
