from __future__ import annotations

import pytest

from vts.api.auth_routes import _safe_next


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.com",
        "http://evil.com/",
        "//evil.com",
        "//evil.com/path",
        "evil.com",
        "javascript:alert(1)",
        "ftp://evil.com",
        # vts-le1: backslash bypass — browsers normalise '\' to '/' in
        # Location:, turning '/\evil.com' into '//evil.com'.
        "/\\evil.com",
        "/\\\\evil.com",
        # vts-le1: percent-encoded slash bypass.
        "/%2fevil.com",
        "/%2f%2fevil.com",
        "/%2F%2Fevil.com",
        "/%5cevil.com",
        "/%5C%5Cevil.com",
    ],
)
def test_safe_next_rejects_dangerous_inputs(value) -> None:
    assert _safe_next(value) == "/"


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/dashboard",
        "/dashboard/",
        "/path/with/segments",
        "/path?query=string",
        "/path#fragment",
    ],
)
def test_safe_next_accepts_local_paths(value) -> None:
    assert _safe_next(value) == value
