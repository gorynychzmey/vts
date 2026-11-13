"""Short-lived signed links to a recording's media (vts-vtma.3).

The MCP Apps player view runs in the host's sandboxed iframe: it carries no
VTS session cookie and cannot put a bearer token on a <video src>. So the view
gets the media through a link that authorises itself — signed for one
recording and one user, valid for minutes — issued by an app-only tool that
went through the normal per-user check.

The key is derived from the session secret with its own purpose label, so a
media signature can never stand in for a session cookie or the reverse, and
no new secret has to be configured.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

# How long a link works. Long enough to start playback and seek around; a view
# that outlives it asks for a fresh one when the media errors.
TTL_SECONDS = 600

_PURPOSE = b"vts media url v1"


def media_signing_key(settings: Any) -> bytes:
    """The HMAC key for media links, from the session secret."""
    from vts.api.main import _resolve_session_secret

    secret = _resolve_session_secret(
        env_secret=settings.session_secret,
        secret_file=settings.session_secret_file,
    )
    return hmac.new(secret.encode("utf-8"), _PURPOSE, hashlib.sha256).digest()


def _signature(key: bytes, recording_id: uuid.UUID, user_id: uuid.UUID, exp: int) -> str:
    message = f"{recording_id}:{user_id}:{exp}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def signed_media_path(
    key: bytes,
    recording_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    now: float | None = None,
    ttl: int = TTL_SECONDS,
) -> tuple[str, int]:
    """The path of a signed media link and its expiry (epoch seconds)."""
    exp = int(now if now is not None else time.time()) + ttl
    sig = _signature(key, recording_id, user_id, exp)
    return f"/api/recordings/{recording_id}/media?u={user_id}&exp={exp}&sig={sig}", exp


def verify_media_signature(
    key: bytes,
    recording_id: uuid.UUID,
    user_id: uuid.UUID,
    exp: int,
    sig: str,
    *,
    now: float | None = None,
) -> bool:
    """True for a link this server signed, for exactly this recording and
    user, and not yet expired. An expiry further out than one TTL was not
    issued here and is refused too: a forged far-future `exp` must not buy a
    permanent link even if the signature check were ever weakened."""
    current = int(now if now is not None else time.time())
    if exp < current or exp > current + TTL_SECONDS + 60:
        return False
    expected = _signature(key, recording_id, user_id, exp)
    return hmac.compare_digest(expected, str(sig))


# --- Any other per-recording file, signed by its path (vts-vtma.6) ---
#
# A voice preview clip is addressed by recording, label and clip index, so the
# signature covers the whole path rather than just the recording id: a link
# for one clip must not open another clip, or the media, of the same recording.

def _path_signature(key: bytes, path: str, user_id: uuid.UUID, exp: int) -> str:
    message = f"path\n{path}\n{user_id}\n{exp}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def signed_path(
    key: bytes,
    path: str,
    user_id: uuid.UUID,
    *,
    now: float | None = None,
    ttl: int = TTL_SECONDS,
) -> tuple[str, int]:
    """`path` with u/exp/sig appended, and its expiry (epoch seconds)."""
    exp = int(now if now is not None else time.time()) + ttl
    sig = _path_signature(key, path, user_id, exp)
    return f"{path}?u={user_id}&exp={exp}&sig={sig}", exp


def verify_path_signature(
    key: bytes,
    path: str,
    user_id: uuid.UUID,
    exp: int,
    sig: str,
    *,
    now: float | None = None,
) -> bool:
    """Same rules as verify_media_signature, for a link made by signed_path."""
    current = int(now if now is not None else time.time())
    if exp < current or exp > current + TTL_SECONDS + 60:
        return False
    return hmac.compare_digest(_path_signature(key, path, user_id, exp), str(sig))
