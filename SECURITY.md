# Security policy

## Reporting a vulnerability

If you find a security issue in vts, **please do not open a public GitHub
issue**. Instead, report it privately:

- Use GitHub's [private vulnerability reporting](https://github.com/gorynychzmey/vts/security/advisories/new)
  (preferred), or
- Email the maintainer at the address listed on the GitHub profile of the
  repository owner.

Please include:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof-of-concept.
- The version (`/api/version` or `vts/__init__.py`) you tested against.

You should expect an initial response within a few days. Critical issues will
be patched and released as quickly as possible; lower-severity findings will be
batched into the next regular release.

## Scope

vts is a self-hosted application. The maintainer can address vulnerabilities
in the code published in this repository, but cannot patch a deployment you
operate yourself — you must redeploy the fixed version.

In-scope:

- Code in this repository (`vts/`, `alembic/`, `scripts/`, `docker/`, `.github/`).
- Default configuration shipped with the repository.

Out of scope:

- Third-party services vts integrates with (Whisper ASR, llama.cpp, Ollama, Postgres, Redis).
- Misconfiguration of your own deployment (exposed admin endpoint, weak proxy headers, etc.).
- Vulnerabilities requiring a privileged attacker (server admin, DB admin).

## Hardening notes

A few things worth checking on any production deployment:

- **`trusted_proxy_cidrs`** must list only your reverse proxy. vts trusts
  `X-Forwarded-User` from any address in this list. A wide CIDR (e.g.
  `0.0.0.0/0`) effectively disables authentication.
- **Admin emails** (`VTS_ADMIN_EMAILS`) grant access to admin-only endpoints
  including user impersonation (`?as_user=...`). Keep this list short.
- **`/api/admin/users`** is admin-only but lists all registered users; do not
  expose admin tooling on a public network.
- **VAPID private key** (push notifications) should be treated as a credential.
  Generate it once, store in your env file, never commit it.
- The metrics JSONL stream contains task IDs and user IDs; treat the file as
  internal log data.
