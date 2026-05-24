from __future__ import annotations

import pytest

from vts.core.config import Settings
from vts.services.web_oauth import build_oauth_client


def test_build_oauth_client_registers_google() -> None:
    settings = Settings(
        oauth_enabled=True,
        oauth_client_id="abc.apps",
        oauth_client_secret="shh",
        public_base_url="https://vts.example",
    )
    oauth = build_oauth_client(settings)
    google = oauth.create_client("google")
    assert google is not None
    assert google.client_id == "abc.apps"
    # server_metadata_url is what triggers OIDC discovery (JWKs, etc.)
    assert "openid-configuration" in (google._server_metadata_url or "")


def test_build_oauth_client_raises_when_disabled() -> None:
    settings = Settings(oauth_enabled=False)
    with pytest.raises(RuntimeError, match="oauth_enabled"):
        build_oauth_client(settings)
