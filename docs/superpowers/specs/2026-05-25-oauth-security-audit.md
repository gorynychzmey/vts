# OAuth Security Audit — vts-4mo

**Date:** 2026-05-25
**Scope:** OAuth implementation end-to-end (web UI cookie session + MCP Bearer)
**Reviewer:** Claude (Opus 4.7) under bd:vts-4mo
**Code baseline:** `main` @ 0879678 (build-1.0.55 + post-release fixes)

## Summary

Six findings: **0 Critical, 2 High, 2 Medium, 2 Low.** No issue blocks
generalising the provider (vts-tlw), but the two High items should be
fixed first because vts-tlw inherits the same surface area.

| # | Severity | Title | File |
|---|----------|-------|------|
| 1 | High | Session cookie not bound to user identity — replay survives logout if cookie leaks | [api/auth_routes.py:85](vts/api/auth_routes.py#L85), [api/main.py:404](vts/api/main.py#L404) |
| 2 | High | `/auth/logout` lacks CSRF protection; SameSite=lax POST is reachable cross-site via form re-submission | [api/auth_routes.py:85](vts/api/auth_routes.py#L85) |
| 3 | Medium | Bearer-token email allow-list bypass via session smuggling when `Authorization` header is malformed | [services/auth.py:44-66](vts/services/auth.py#L44-L66) |
| 4 | Medium | Session secret defaults to deterministic derivation from `oauth_client_secret` — same input across hosts → same secret | [api/main.py:374](vts/api/main.py#L374) |
| 5 | Low | Open-redirect bypass via backslash and percent-encoded slash in `next` | [api/auth_routes.py:26-36](vts/api/auth_routes.py#L26-L36) |
| 6 | Low | `as_user` query-param admin impersonation accepts arbitrary strings without normalisation | [services/auth.py:80-99](vts/services/auth.py#L80-L99) |

The threat model and remaining checklist items are at the end.

---

## Finding 1 — Session cookie not bound to user identity (High)

### Where

- `vts_session` cookie set by `SessionMiddleware` in [api/main.py:404-412](vts/api/main.py#L404-L412)
- Read by the session branch of `resolve_user_from_request` in [services/auth.py:62-66](vts/services/auth.py#L62-L66)
- Cleared by `/auth/logout` in [api/auth_routes.py:85-88](vts/api/auth_routes.py#L85-L88)

### Issue

`/auth/logout` only clears the `email` key from the current request's
session dict (line 87). It does NOT invalidate the cookie itself —
Starlette's `SessionMiddleware` re-signs the (now empty) session on the
response. The original signed cookie still verifies as authentic and
still contains `{"email": "<user>"}` until `max_age` (30 days) expires.

Concretely: if a `vts_session` cookie is captured (XSS in a future
feature, shared workstation, browser-extension exfiltration, off-host
log, etc.) AFTER the user has clicked Logout, the attacker can still
authenticate by replaying the captured cookie value — because the
server has no record that this particular cookie was logged out. The
allow-list gate only fires at `/auth/callback`, not on every session-
branch request ([services/auth.py:65](vts/services/auth.py#L65) "Allow-list was enforced at /auth/callback; trust the session.").

This also means that removing a user from `oauth_allowed_emails` /
`oauth_allowed_domains` does NOT revoke their existing sessions — they
keep working for up to 30 days.

### Exploit scenario

1. Alice logs in on a shared/compromised browser; cookie is captured.
2. Alice removes herself from `oauth_allowed_domains` (or admin does).
3. Attacker replays the captured cookie → access granted for 30 days.

### Recommendation

Two layered fixes, in order of importance:

1. **Re-check the allow-list in the session branch on every request.**
   Cheap (it's an in-memory set lookup) and closes the revocation gap.
2. **Bind sessions to a server-side record.** Either:
   - Store a per-login `session_id` (random 128 bits) in the cookie and
     a `(session_id → email, issued_at)` row in DB/Redis; `/auth/logout`
     deletes the row. Cookie alone is no longer sufficient.
   - Or include an `iat` (issued-at) in the cookie and a per-user
     `min_session_iat` server-side that Logout bumps to "now"; reject
     cookies older than that.

Track as a follow-up `bd` issue — fix before vts-tlw, since the
pluggable provider abstraction should not bake in cookie-only session
semantics.

---

## Finding 2 — `/auth/logout` reachable cross-site (High)

### Where

[api/auth_routes.py:85-88](vts/api/auth_routes.py#L85-L88)

### Issue

`/auth/logout` is `POST` with no CSRF token and no `Origin`/`Referer`
check. The session cookie is `SameSite=lax`, which **does** block
cross-site `POST` from a third-party form in modern browsers — BUT:

- Old browsers without `SameSite=lax` enforcement (pre-2020 mobile
  WebViews still in the wild) send the cookie on cross-site POST.
- `SameSite=lax` allows the cookie on a *top-level* navigation that is
  a `GET`. An attacker page that does `<form action="/auth/logout"
  method="POST"><input type=submit></form>` does NOT bypass lax for
  `POST` — but `lax` is not the same defence as a CSRF token, and the
  endpoint is otherwise unauthenticated, so the cookie attaches.

Logout CSRF is normally rated Low. It's rated High here only because
the same endpoint pattern is the template for any future state-changing
endpoint added by vts-tlw (provider switch, key rotation, allow-list
edits). Establishing the CSRF discipline now — before the surface
grows — is cheaper than retrofitting.

### Exploit scenario

Low-impact directly: attacker page forces victim's session to
terminate. Annoying, not catastrophic. The reason to fix now is
**precedent**: when vts-tlw adds a "rotate client secret" or
"switch provider" admin endpoint, the same pattern will be applied
and the impact won't be limited to a forced logout.

### Recommendation

Either:

- Add a `Sec-Fetch-Site` check (reject anything other than `same-origin`
  / `none`) — works on all modern browsers.
- Or require a CSRF token (double-submit cookie or signed token in a
  hidden field) for all state-changing endpoints under `/auth/*`.

Document the chosen approach in the vts-tlw design as the standard for
all future state-changing endpoints.

---

## Finding 3 — Bearer allow-list bypass via header smuggling (Medium)

### Where

[services/auth.py:44-66](vts/services/auth.py#L44-L66)

### Issue

The branch selector uses a case-insensitive prefix match:

```python
auth_header = request.headers.get("authorization", "")
if auth_header.lower().startswith("bearer "):
    token = get_access_token()
    ...
```

If `Authorization` is present but malformed (e.g. `Bearer xyz` where
`xyz` is not a valid FastMCP-issued token), `get_access_token()`
returns `None` and we 401 — that part is safe.

The actual issue is the **fall-through**: when `Authorization` does NOT
start with `bearer ` but a `vts_session` cookie IS present, the request
takes the session branch and bypasses `is_email_allowed`. That's by
design (the cookie was already allow-listed at callback time), but it
means a malicious MCP client can:

1. Authenticate via the browser flow → get a session cookie.
2. Make subsequent MCP `/mcp` requests with the cookie attached
   (browsers do this automatically) and **omit** `Authorization`.
3. The request takes the session branch — bypassing the Bearer-token
   allow-list re-check on the MCP code path.

For the current single-user deployment with one allow-list shared
between web and MCP, this is equivalent. But the threat model for
vts-tlw probably wants distinct allow-lists per channel (e.g. MCP
restricted to a stricter subset), and the current resolver makes that
impossible to express.

### Exploit scenario

Future state: admin tightens MCP allow-list to a single email but
keeps the web allow-list broad. Any web-allow-listed user can now
make MCP calls by simply omitting `Authorization` and letting the
browser-issued cookie ride.

### Recommendation

- Decide explicitly: should MCP requests accept cookie auth at all?
  Today the only legitimate MCP client (Claude Desktop, claude.ai,
  ChatGPT) speaks Bearer, never cookie. Rejecting cookie auth on the
  `/mcp` mount eliminates cross-flow smuggling entirely.
- Implementation sketch: in `resolve_user_from_request`, check
  `request.url.path.startswith(settings.mcp_path)` and require Bearer
  on that path; outside `/mcp`, accept either.

Track as part of vts-tlw — the resolver shape is what makes this
ambiguous and that's exactly what vts-tlw will rework.

---

## Finding 4 — Deterministic session-secret derivation (Medium)

### Where

[api/main.py:374-378](vts/api/main.py#L374-L378)

```python
session_secret = settings.session_secret or hashlib.blake2b(
    settings.oauth_client_secret.encode("utf-8"),
    key=b"vts-session-cookie",
    digest_size=32,
).hexdigest()
```

### Issue

When `VTS_SESSION_SECRET` is unset, the session-cookie HMAC key is
derived deterministically from `oauth_client_secret`. Two consequences:

1. **No per-deploy entropy.** If the same OAuth client (same
   `client_secret`) is reused across staging + prod (common during
   bootstrap, and easy to do by accident with one set of secrets), the
   session cookies are interchangeable between environments. A
   staging-issued cookie authenticates prod and vice versa.
2. **Rotating `oauth_client_secret` silently invalidates all live
   sessions.** Issue text flags this as "intended" but it's worth
   noting: the operator may not realise that rotating Google's client
   secret will log out every user. Make this explicit in docs.
3. **`oauth_client_secret` is also passed verbatim to Google over TLS
   on every code exchange.** It already has high-confidentiality
   handling — but using it as the *only* input to the session HMAC
   means a single leak of `oauth_client_secret` lets an attacker forge
   arbitrary session cookies (including for emails outside the
   allow-list, defeating Finding 1's recommended re-check).

The `blake2b` keyed hash itself is fine — the issue is the input.

### Recommendation

- Make `VTS_SESSION_SECRET` *required* when `oauth_enabled=True` (fail
  startup if unset). Generate via `python -c 'import secrets;
  print(secrets.token_hex(32))'` and store in the host env file.
- Drop the fallback derivation — it's a footgun, not a convenience.
- Document the rotation semantics: rotating the session secret logs
  everyone out; rotating the OAuth client secret no longer does
  (they're now independent).

---

## Finding 5 — `_safe_next` open-redirect bypass (Low)

### Where

[api/auth_routes.py:26-36](vts/api/auth_routes.py#L26-L36)

```python
def _safe_next(value: str | None) -> str:
    if not value:                       return "/"
    if not value.startswith("/"):       return "/"
    if value.startswith("//"):          return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:  return "/"
    return value
```

### Issue

The function handles the obvious cases (`//evil.com`, `https://evil.com`,
relative URLs) but I tested two bypasses against the logic:

1. **Backslash:** `/\evil.com`. Starts with `/`, doesn't start with
   `//`, `urlparse` returns `scheme=''` and `netloc=''` (Python treats
   backslash as path content, not a separator). The function returns
   `/\evil.com`. Most modern browsers normalise `\` to `/` BEFORE
   following a `Location:` header, so the redirect target becomes
   `//evil.com` → cross-origin. Confirmed pattern; same class as
   CVE-2017-1000028.

2. **Percent-encoded slash:** `/%2f%2fevil.com`. Starts with `/`,
   doesn't start with `//` (the second char is `%`, not `/`).
   `urlparse` doesn't decode percent-encoding. Returns `/%2f%2fevil.com`.
   Sent to `RedirectResponse` which emits it verbatim in `Location:`.
   Browser behaviour varies — Chrome historically followed it as an
   absolute redirect; current Chrome does not. Defence-in-depth: still
   worth blocking.

### Exploit scenario

Phishing: attacker sends victim
`https://vts.example.com/auth/login?next=/\evil.com`. After
successful Google login the user is redirected to `evil.com` (which
hosts a copy of the vts UI asking for credentials again, or hosts an
OAuth-grant-confusion attack against a different RP).

### Recommendation

After the existing checks, also reject:
```python
if "\\" in value:              return "/"
if "%2f" in value.lower():     return "/"
if "%5c" in value.lower():     return "/"
```
Or simpler: canonicalise once via `posixpath.normpath` and reject if
the result doesn't equal the input (after a leading-slash
preservation).

Severity is Low because the redirect happens only after a successful
Google login — the attacker doesn't get the victim's session, just
their next-page navigation. Still worth fixing.

---

## Finding 6 — `as_user` admin impersonation accepts arbitrary strings (Low)

### Where

[services/auth.py:80-99](vts/services/auth.py#L80-L99)

### Issue

When an admin passes `?as_user=<value>`, the value is stripped of
surrounding whitespace and used verbatim as the lookup key in
`repo.get_user_by_username`. No case-normalisation, no email-shape
check. Two implications:

1. The User table stores usernames as set by `get_or_create_user`,
   which receives `email.strip().lower()` from
   `/auth/callback`. So all stored usernames are lowercase emails. An
   admin who passes `?as_user=Alice@vostrikov.de` gets a 404 even
   though `alice@vostrikov.de` exists — confusing UX but not a
   security bug.
2. There's no validation that `as_user` looks like an email at all.
   `?as_user=admin` or `?as_user=' OR 1=1 --` will hit the parameterised
   SQLAlchemy query (so no SQLi) and 404. But if any future code path
   creates non-email usernames, admin could impersonate them via this
   route.

### Recommendation

- Apply `.strip().lower()` to `as_user` before the lookup so the
  matching is consistent with `/auth/callback`.
- Optionally reject values without `@` to make the contract explicit
  ("admin can act as any registered email, not any string").

Severity Low because the gate (`is_admin`) is the actual security
boundary and that gate works.

---

## Items checked, no finding

These were on the issue's scope checklist and came out clean:

- **Cookie flags** ([api/main.py:404-412](vts/api/main.py#L404-L412)): `HttpOnly` ✓ (Starlette default),
  `https_only=True` (= Secure) ✓, `same_site="lax"` ✓.
- **`_safe_next` against `//evil.com`, `https://evil.com`, raw netloc**: all
  collapse to `/` correctly.
- **`/auth/callback` allow-list ordering**: allow-list runs at
  [auth_routes.py:67-72](vts/api/auth_routes.py#L67-L72), BEFORE the DB write at [auth_routes.py:74-78](vts/api/auth_routes.py#L74-L78). Anonymous
  users cannot pollute the User table via repeated callbacks.
- **Allow-list email matching**: case-insensitive, whitespace-stripped
  ([mcp/allowlist.py:18-25](vts/mcp/allowlist.py#L18-L25)). Unicode-confusable emails are NOT normalised
  (Greek α vs Latin a) — this is consistent with Google's own
  behaviour and the standard recommendation (don't try to defend
  against unicode-confusable; control the allow-list contents).
- **`get_or_create_user` TOCTOU**: race exists but DB unique constraint
  on `User.username` ([db/models.py:52](vts/db/models.py#L52)) makes the loser raise
  `IntegrityError` rather than producing duplicates.
- **MCP Bearer token re-validation**: `get_access_token()` returns
  cached claims; FastMCP's `GoogleProvider` validates id_token sig +
  aud + iss at handshake (the `/token` exchange) but does NOT re-verify
  on every MCP request. This is per the OAuth resource-server pattern
  (Bearer token is the credential, not the id_token) and is fine
  PROVIDED the allow-list is re-checked per-request — which the bearer
  branch DOES at [services/auth.py:52-58](vts/services/auth.py#L52-L58). ✓
- **Token replay after allow-list removal**: bearer branch re-runs
  `is_email_allowed` every request, so removing a domain takes effect
  on the next call. ✓ (Web session branch does NOT — that's Finding 1.)
- **DCR `/register` filling the OAuth client cache**: not exploitable
  beyond a DoS-class resource consumption; per the audit rules DoS is
  out of scope.
- **Logging**: `grep -rn "logger\|logging" vts/api/auth_routes.py
  vts/services/auth.py` — neither file logs tokens, codes, or
  secrets. Errors include exception messages from authlib which may
  contain Google's error response (no token material). ✓
- **Reverse-proxy bypass**: no Traefik middleware bypass rules in the
  repo. `X-Forwarded-User` is trusted ONLY when `oauth_enabled=False`
  ([services/auth.py:38-42](vts/services/auth.py#L38-L42)) — dev-mode only, explicitly documented.

---

## Threat model snapshot (pre-vts-tlw)

| Asset | Threat | Mitigation today | Gap |
|-------|--------|------------------|-----|
| User content (transcripts, summaries) | Unauthorised read | Per-user `acting_as` scoping in repo queries | None new |
| Web UI access | Forged session | HMAC-signed cookie, allow-list at login | Findings 1, 4 |
| MCP access | Stolen bearer | FastMCP token issuance + per-request allow-list | Finding 3 (smuggling) |
| Admin impersonation | Lateral movement | `is_admin` check on `as_user` | Finding 6 (input shape) |
| OAuth client_secret | Provider takeover | Env-file only, never in repo/logs | Finding 4 (secret reuse for sessions) |

When vts-tlw lands, the pluggable provider abstraction MUST:

1. Keep allow-list enforcement at the resolver layer, not the provider
   layer (so swapping Google → GitHub doesn't accidentally drop the
   allow-list).
2. Treat session-secret and provider-secret as independent inputs
   (Finding 4).
3. Either reject cookie auth on `/mcp` or document why it's accepted
   (Finding 3).
4. Re-check allow-list on every session-branch request (Finding 1).

## Follow-up bd issues

Recommended to file:

- **High:** Session revocation gap (Finding 1) — implement server-side
  session record OR per-request allow-list re-check. Block vts-tlw on
  the resolver-side mitigation.
- **High:** `Sec-Fetch-Site` (or CSRF-token) discipline for all
  state-changing endpoints under `/auth/*` (Finding 2). Set the
  pattern before vts-tlw expands the surface.
- **Medium:** Make `VTS_SESSION_SECRET` required when
  `oauth_enabled=True` and drop the deterministic fallback (Finding 4).
- **Medium:** Decide MCP cookie-auth policy and enforce in the
  resolver (Finding 3). Belongs inside vts-tlw scope.
- **Low:** Harden `_safe_next` against `\` and `%2f` (Finding 5).
- **Low:** Normalise `as_user` to `.strip().lower()` (Finding 6).
