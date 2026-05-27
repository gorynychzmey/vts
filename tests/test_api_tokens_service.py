from __future__ import annotations

from vts.services.api_tokens import (
    PREFIX_DISPLAY_LEN,
    TOKEN_PREFIX,
    generate_token,
    hash_token,
    looks_like_api_token,
    token_prefix,
)


def test_generate_token_has_prefix() -> None:
    t = generate_token()
    assert t.startswith(TOKEN_PREFIX)
    assert looks_like_api_token(t)


def test_generate_token_is_random() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50


def test_generate_token_is_long_enough() -> None:
    # >= 256 bits of entropy → at least ~43 base64url chars on top of prefix
    t = generate_token()
    body = t[len(TOKEN_PREFIX):]
    assert len(body) >= 40


def test_hash_token_is_deterministic_and_hex() -> None:
    t = "vts_AAAA"
    h = hash_token(t)
    assert h == hash_token(t)
    assert len(h) == 64
    int(h, 16)  # must be valid hex


def test_hash_token_differs_per_input() -> None:
    assert hash_token("vts_AAAA") != hash_token("vts_BBBB")


def test_token_prefix_truncates_to_display_length() -> None:
    t = generate_token()
    p = token_prefix(t)
    assert len(p) == PREFIX_DISPLAY_LEN
    assert p.startswith(TOKEN_PREFIX)
    assert t.startswith(p)


def test_looks_like_api_token_rejects_oauth_bearer() -> None:
    # FastMCP OAuth access tokens are opaque JWT-ish strings, never start with vts_
    assert not looks_like_api_token("ya29.a0AfH6...")
    assert not looks_like_api_token("eyJhbGciOiJSUzI1NiJ9.foo.bar")
    assert not looks_like_api_token("")
