from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "vts_"
_RAW_BYTES = 32
PREFIX_DISPLAY_LEN = 12  # "vts_" + 8 chars of body


def generate_token() -> str:
    """Return a fresh raw API token of shape "vts_<43-char-url-safe-base64>"."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_RAW_BYTES)


def looks_like_api_token(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX)


def hash_token(raw: str) -> str:
    """SHA-256 hex of the raw token. Used as the DB key.

    Encodes as UTF-8, not ASCII: Starlette decodes header values as latin-1, so
    `Authorization: Bearer vts_\\xff` arrives as a non-ASCII str and
    `.encode("ascii")` raised UnicodeEncodeError. Nothing caught it, so junk
    bytes from an unauthenticated client produced a 500 and a traceback instead
    of a 401 (vts-cy1). The hash input is arbitrary bytes anyway.

    UTF-8 and ASCII agree byte-for-byte on ASCII input, so digests of existing
    tokens are unchanged and stored hashes stay valid.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_prefix(raw: str) -> str:
    """First PREFIX_DISPLAY_LEN chars of the raw token; safe to store and show."""
    return raw[:PREFIX_DISPLAY_LEN]
