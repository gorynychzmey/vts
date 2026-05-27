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
    """SHA-256 hex of the raw token. Used as the DB key."""
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def token_prefix(raw: str) -> str:
    """First PREFIX_DISPLAY_LEN chars of the raw token; safe to store and show."""
    return raw[:PREFIX_DISPLAY_LEN]
