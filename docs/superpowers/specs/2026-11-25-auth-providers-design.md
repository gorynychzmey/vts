# Pluggable login provider (Google / Microsoft Entra ID / generic OIDC) — design

Status: approved by Victor 2026-11-25 · bd: `vts-sf93.1` (absorbs `vts-tlw`)

## Intent

German SMB customers live in Microsoft 365; a Google-only login closes the
door at the first meeting. An installation must be configurable to log in with
Microsoft Entra ID (the customer's tenant) — for the web UI **and** for MCP
connectors (claude.ai, ChatGPT) — without any Google client. Self-hosters with
Keycloak, Authentik, Zitadel etc. get a generic OIDC option on the same
mechanism (`vts-tlw`).

## Decisions (Victor)

- **One provider per installation**, chosen by configuration. Several at once
  (a provider picker page, an aggregating MCP proxy) is out of scope until a
  customer needs it.
- No Entra tenant is available yet: real Entra login is verified later, at the
  first customer; this work ships with tests against fakes and documentation.

## Design

- **Setting** `VTS_AUTH_PROVIDER` = `google` (default — existing installs see no
  change) | `entra` | `oidc`. `oauth_enabled` keeps its meaning (login on/off).
  - `google`: the existing `VTS_OAUTH_CLIENT_ID` / `VTS_OAUTH_CLIENT_SECRET`.
  - `entra`: `VTS_ENTRA_TENANT_ID`, `VTS_ENTRA_CLIENT_ID`,
    `VTS_ENTRA_CLIENT_SECRET`, and `VTS_ENTRA_API_SCOPE` (the app's exposed API
    scope the MCP connector requests, e.g. `api://<client-id>/vts`).
  - `oidc`: `VTS_OIDC_DISCOVERY_URL`, `VTS_OIDC_CLIENT_ID`,
    `VTS_OIDC_CLIENT_SECRET`, `VTS_OIDC_EMAIL_CLAIM` (default `email`),
    optional `VTS_OIDC_SCOPES` (default `openid email profile`).
  - Missing required values for the chosen provider → a clear startup error
    naming the variables (as today for Google).
- **One provider module** (`vts/services/auth_providers.py`) is the only place
  that knows provider differences. It gives:
  - the web half: OIDC metadata URL + scopes for authlib (Google's discovery
    URL; `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`;
    or the configured discovery URL);
  - the MCP half: a FastMCP auth provider (`GoogleProvider`, `AzureProvider`,
    `OIDCProxy`) with the existing persistent client storage, redirect path
    `/mcp/auth/callback` and consent mode;
  - `email_from_claims(claims) -> str | None`: Google/OIDC → the configured
    claim (`email`); Entra → `email`, else `preferred_username`, else `upn`.
    Lower-cased, trimmed.
  `auth_routes.py`, `web_oauth.py` and `build_mcp_server` stop naming Google.
- **Identity, allow-list, sessions, API tokens, admin impersonation** are
  unchanged: the user is still keyed by the lower-cased email, checked against
  `oauth_allowed_emails` / `oauth_allowed_domains`. The MCP bearer path in
  `resolve_user_from_request` uses `email_from_claims` instead of reading
  `email` directly.
- **No login page change**: `/auth/login` redirects straight to the configured
  provider, as it does to Google today.
- **Docs** (`docs/AUTH.md`): provider-neutral flow, the new settings, Entra app
  registration (web + MCP redirect URIs, exposed API scope, client secret,
  tenant id), a Keycloak/Authentik example for `oidc`, and how the email is
  derived. Every documented variable is verified by actually loading it.

## Approaches considered

- External auth proxy (oauth2-proxy) in front of VTS — rejected: MCP
  connectors need VTS's own OAuth server (FastMCP), which a proxy cannot give.
- Generic OIDC for everything, Google and Entra included — rejected for MCP:
  Google access tokens are opaque and cannot be verified by `OIDCProxy`; the
  dedicated `GoogleProvider` is required.
- **Chosen:** dedicated FastMCP providers for Google and Entra plus `OIDCProxy`
  for the rest, behind one module that also feeds the web login.

## Non-goals

- Several providers at once; per-user provider choice; SCIM/user provisioning;
  Entra group-based authorisation (the allow-list stays email/domain based).

## Constraints

- Public repo rules; English in code/docs; version: minor bump 1.12.0 (new
  functionality).
- Backward compatible: an install with only today's settings behaves exactly as
  before (Google).
