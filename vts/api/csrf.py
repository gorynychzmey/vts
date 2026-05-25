from __future__ import annotations

from fastapi import HTTPException, Request

_SAFE_SEC_FETCH_SITE = frozenset({"same-origin", "same-site", "none"})


def require_same_site(request: Request) -> None:
    """Reject cross-site state-changing requests via Sec-Fetch-Site.

    Modern browsers (~2020+) attach `Sec-Fetch-Site` to every request.
    For cross-origin attacker pages the value is `cross-site`; for
    legitimate same-origin form/fetch it is `same-origin`. Same-site
    (subdomains) and `none` (user-typed URL / extension) are also
    treated as safe.

    Fail-closed: a request without the header is rejected. vts is
    self-hosted and targets modern browsers; pre-2020 browsers without
    `Sec-Fetch-Site` would be unable to perform state-changing actions
    here, which is acceptable.

    Use as a FastAPI dependency on POST/PUT/DELETE endpoints under
    /auth/* and any future state-changing admin endpoint.
    """
    value = request.headers.get("sec-fetch-site")
    if value is None:
        raise HTTPException(status_code=403, detail="Sec-Fetch-Site header required")
    if value not in _SAFE_SEC_FETCH_SITE:
        raise HTTPException(status_code=403, detail="Cross-site request blocked")
