# Pluggable login provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One configured login provider — Google (default), Microsoft Entra ID or generic OIDC — for both the web UI and MCP OAuth.

**Architecture:** `vts/services/auth_providers.py` maps the configured provider to (a) authlib registration data for the web login, (b) a FastMCP auth provider for MCP, (c) an email-from-claims rule. Callers stop naming Google.

**Tech Stack:** Python 3 / FastAPI / authlib / FastMCP 3.3 / pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-11-25-auth-providers-design.md`

## Global Constraints

- Tests on Postgres where a DB is needed: `VTS_TEST_DATABASE_URL=postgresql+asyncpg://vts:vts@localhost:5432/vts_test_lzad .venv/bin/python -m pytest` (never another DB).
- Backward compatibility: with no new settings, behaviour, routes and env names are exactly today's (Google). Existing tests stay green unchanged.
- Version: Task 1 bumps to **1.12.0**.
- No network in tests: authlib metadata fetches and FastMCP providers are exercised with fakes/monkeypatching; do not contact Google/Microsoft.
- Public repo rules (no private hosts/paths); English; reference `vts-sf93.1`.
- Narrow `git add`; never commit `tests/ui/node_modules`.

## Review Focus

1. **Entra user without an `email` claim** → email from `preferred_username`/`upn`, lower-cased; the allow-list applies to it.
2. **Misconfiguration** (provider `entra` without tenant, `oidc` without discovery URL, unknown provider name) → startup error naming the missing variables, not a 500 at first login.
3. **Default install** (only `VTS_OAUTH_*`) behaves exactly as today in web login and MCP.
4. **OIDC with a custom email claim** (e.g. `mail`) and a token missing it → 401/400 with a clear message, never a user keyed by an empty string.

---

### Task 1: settings and the provider module

**Files:**
- Modify: `vts/core/config.py` (new fields with `VTS_` env aliases as the existing ones are declared; validation)
- Create: `vts/services/auth_providers.py`
- Test: `tests/test_auth_providers.py`

**Interfaces:**
- Settings: `auth_provider: Literal["google","entra","oidc"] = "google"`; `entra_tenant_id`, `entra_client_id`, `entra_client_secret`, `entra_api_scope`; `oidc_discovery_url`, `oidc_client_id`, `oidc_client_secret`, `oidc_email_claim = "email"`, `oidc_scopes = "openid email profile"` (all `str | None` unless defaulted).
- `auth_providers.py`:
  - `class AuthConfigError(RuntimeError)`.
  - `validate_auth_settings(settings) -> None` — when `oauth_enabled`, raise `AuthConfigError` naming every missing variable for the chosen provider (and `VTS_PUBLIC_BASE_URL`).
  - `web_oidc_registration(settings) -> dict` — kwargs for `authlib OAuth.register`: `name` (the provider name), `client_id`, `client_secret`, `server_metadata_url`, `client_kwargs={"scope": ...}`. Google: current metadata URL + `openid email`; Entra: `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration` + `openid email profile`; OIDC: discovery URL + `oidc_scopes`.
  - `email_from_claims(settings, claims: Mapping) -> str | None` — Google: `email`; OIDC: `claims[oidc_email_claim]`; Entra: `email` → `preferred_username` → `upn`; trimmed, lower-cased; empty → None.
  - `build_mcp_auth_provider(settings, client_storage) -> AuthProvider` — Google: `GoogleProvider(...)` with today's arguments (unchanged); Entra: `AzureProvider(client_id, client_secret, tenant_id, required_scopes=[<api scope name>], base_url, redirect_path, client_storage, require_authorization_consent="remember", identifier_uri derived from the API scope)`; OIDC: `OIDCProxy(config_url=discovery, client_id, client_secret, base_url, redirect_path, client_storage, require_authorization_consent="remember")` — check the installed FastMCP signatures and map accordingly.

- [ ] Failing tests: validation messages per provider (names the missing env vars); registration dicts per provider; email rules incl. Review Focus 1 and 4; `build_mcp_auth_provider` returns the right class with the right key arguments (monkeypatch the classes to capture kwargs — no network) → implement → full backend suite → **Commit** (with 1.12.0 bump) `feat(auth): pluggable login provider settings and module (vts-sf93.1)`.

### Task 2: web login and MCP use the provider module

**Files:**
- Modify: `vts/services/web_oauth.py` (`build_oauth_client` registers from `web_oidc_registration`), `vts/api/auth_routes.py` (create the client by the registered name; email via `email_from_claims` from `token["userinfo"]`, falling back to the ID token claims; error text provider-neutral), `vts/mcp/server.py` (`build_mcp_server` calls `validate_auth_settings` + `build_mcp_auth_provider`), `vts/services/auth.py` (bearer path uses `email_from_claims(settings, token.claims)`), app startup (call `validate_auth_settings` where settings are validated today so misconfig fails at boot)
- Test: `tests/test_auth_provider_wiring.py` + existing auth/MCP tests unchanged

**Interfaces:**
- No route, env or response changes for the Google default.

- [ ] Failing tests: Entra-configured callback with userinfo lacking `email` but with `preferred_username` creates/logs in that user (authlib client faked); allow-list rejection for a non-listed domain; MCP bearer claims with only `upn` resolve the user; `build_mcp_server` with `entra` builds `AzureProvider` (captured); default Google path untouched (existing tests) → implement → full backend suite → **Commit** `feat(auth): web login and MCP OAuth go through the configured provider (vts-sf93.1)`.

### Task 3: documentation

**Files:**
- Modify: `docs/AUTH.md` (provider-neutral flow; settings table for all providers; "Microsoft Entra ID setup" — app registration, redirect URIs `<base>/auth/callback` and `<base>/mcp/auth/callback`, expose an API scope and set `VTS_ENTRA_API_SCOPE`, client secret, tenant id, which claim becomes the user; "Generic OIDC (Keycloak / Authentik)" example), `docs/ARCHITECTURE.md` settings table, `docs/INITIAL_DEPLOYMENT.md` pointer
- Test: `tests/test_auth_docs_keys.py` — every `VTS_*` auth variable named in AUTH.md loads into `Settings` (set it in the environment, read the field)

- [ ] Failing test (docs key check) → write docs → full backend suite → **Commit** `docs(auth): Entra ID and generic OIDC setup (vts-sf93.1)`.

### Release checkpoint
- [ ] Whole-branch review; fix wave; suites; ask Victor for merge + `build-1.12.0`; after deploy verify prod (Google) login still works; close `vts-tlw` as absorbed; leave `vts-sf93.1` open with a note "real Entra verification pending a tenant" or close per Victor.
