"""The MCP OAuth store must outlive the container.

FastMCP's default keeps client registrations and refresh tokens under a
platformdirs path (/root/.local/share) that lives in the container's ephemeral
layer. Every image update wiped it, so every MCP client had to authorise again
after each release — which is the bug these tests exist to keep fixed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


class _Settings:
    def __init__(self, root: Path, secret: str = "test-client-secret-long-enough"):
        self.mcp_oauth_state_dir = root
        self.oauth_client_secret = secret


def _fingerprint(secret: str) -> str:
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    jwt_key = derive_jwt_key(
        high_entropy_material=secret, salt="fastmcp-jwt-signing-key"
    )
    encryption_key = derive_jwt_key(
        high_entropy_material=jwt_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )
    return hashlib.sha256(encryption_key).hexdigest()[:12]


def test_store_lands_under_the_configured_directory(tmp_path):
    """Not under platformdirs — that is the whole point."""
    from vts.mcp.server import _persistent_oauth_storage

    assert _persistent_oauth_storage(_Settings(tmp_path)) is not None
    fp = _fingerprint("test-client-secret-long-enough")
    assert (tmp_path / "oauth-proxy" / fp).is_dir()


def test_directory_is_owner_only(tmp_path):
    """It holds live refresh tokens to the user's Google account."""
    from vts.mcp.server import _persistent_oauth_storage

    _persistent_oauth_storage(_Settings(tmp_path))
    for path in (tmp_path, tmp_path / "oauth-proxy"):
        assert path.stat().st_mode & 0o077 == 0, f"{path} is group/world accessible"


def test_path_matches_what_fastmcp_would_have_used(tmp_path):
    """The fingerprint keeps FastMCP's own scheme.

    An existing store must stay readable when this code takes over, or turning
    the fix on would log everyone out — the opposite of the intent. Verified
    against production: the derivation reproduced the live directory name.
    """
    from vts.mcp.server import _persistent_oauth_storage

    secret = "some-other-secret-value-here"
    _persistent_oauth_storage(_Settings(tmp_path, secret))
    assert (tmp_path / "oauth-proxy" / _fingerprint(secret)).is_dir()


def test_a_different_secret_gets_a_different_directory(tmp_path):
    """Rotating the secret must not silently reuse an undecryptable store."""
    from vts.mcp.server import _persistent_oauth_storage

    _persistent_oauth_storage(_Settings(tmp_path, "secret-number-one-here"))
    _persistent_oauth_storage(_Settings(tmp_path, "secret-number-two-here"))
    assert len(list((tmp_path / "oauth-proxy").iterdir())) == 2


@pytest.mark.asyncio
async def test_values_are_encrypted_at_rest(tmp_path):
    from vts.mcp.server import _persistent_oauth_storage

    store = _persistent_oauth_storage(_Settings(tmp_path))
    await store.put(collection="c", key="k", value={"refresh": "SUPERSECRET"})
    assert await store.get(collection="c", key="k") == {"refresh": "SUPERSECRET"}
    on_disk = "".join(
        p.read_text(errors="ignore")
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert "SUPERSECRET" not in on_disk, "refresh token written in plaintext"


def test_failure_falls_back_instead_of_blocking_startup(tmp_path):
    """Forgetful auth beats a server that will not boot."""
    from vts.mcp.server import _persistent_oauth_storage

    class _Broken:
        mcp_oauth_state_dir = tmp_path / "x"
        oauth_client_secret = None  # derivation raises

    assert _persistent_oauth_storage(_Broken()) is None
