from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vts.api.csrf import require_same_site


def _make_request(headers: dict[str, str] | None = None) -> Request:
    headers_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "headers": headers_list,
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "scheme": "https",
    }
    return Request(scope)


def test_same_origin_allowed() -> None:
    require_same_site(_make_request({"Sec-Fetch-Site": "same-origin"}))


def test_same_site_allowed() -> None:
    require_same_site(_make_request({"Sec-Fetch-Site": "same-site"}))


def test_none_allowed() -> None:
    """User typed the URL directly / opened from bookmark / extension."""
    require_same_site(_make_request({"Sec-Fetch-Site": "none"}))


def test_cross_site_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        require_same_site(_make_request({"Sec-Fetch-Site": "cross-site"}))
    assert exc.value.status_code == 403
    assert "cross-site" in exc.value.detail.lower()


def test_missing_header_rejected() -> None:
    """Fail-closed: legacy browsers without Sec-Fetch-Site cannot perform
    state-changing actions; vts targets modern browsers."""
    with pytest.raises(HTTPException) as exc:
        require_same_site(_make_request())
    assert exc.value.status_code == 403


def test_unknown_value_rejected() -> None:
    """Defensive: any value we don't recognise is treated as cross-site."""
    with pytest.raises(HTTPException) as exc:
        require_same_site(_make_request({"Sec-Fetch-Site": "bogus"}))
    assert exc.value.status_code == 403
