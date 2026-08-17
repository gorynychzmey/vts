"""Cache-Control on bundled static assets.

Assets under /static are version-addressed — index.html references them as
`?v=<app version>`, and fonts change name when their content changes — so they
can be cached hard. Without an explicit header they still cached, but only via
ETag revalidation: a conditional request per asset per page load, just to be
told 304.

The pairing is what matters and is easy to break in either direction: the
assets must be immutable, and the two documents that decide which asset URLs
the browser asks for next — index.html and the service worker — must not be.
Caching either of those would strand users on an old build.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "false")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.test")
    from vts.api.main import app

    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/static/app.js",
        "/static/styles.css",
        "/static/status-predicates.js",
        "/static/icons/icon-192.png",
    ],
)
def test_static_assets_are_immutable(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers.get("cache-control") == IMMUTABLE


def test_index_is_never_cached(client: TestClient) -> None:
    """index.html carries the `?v=` query for every other asset.

    Cache it and the browser keeps asking for the previous release's URLs, so
    a deploy never reaches the user — the failure mode this whole pairing
    exists to avoid.
    """
    response = client.get("/")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert "immutable" not in cache_control


def test_service_worker_is_never_cached(client: TestClient) -> None:
    """A cached service worker outlives the deploy that was meant to replace it."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert "immutable" not in cache_control


def test_conditional_request_still_works(client: TestClient) -> None:
    """ETag revalidation must keep working for anyone who asks for it.

    `immutable` stops the browser from asking, but a forced reload still can,
    and it should get a cheap 304 rather than the whole file.
    """
    response = client.get("/static/app.js")
    etag = response.headers.get("etag")
    assert etag, "static assets must still carry an ETag"
    revalidated = client.get("/static/app.js", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
