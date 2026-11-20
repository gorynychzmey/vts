"""The session secret: the SessionMiddleware HMAC key, and the root other
server-side keys are derived from (signed media links, vts-vtma.3).

Lives in `core` rather than `vts.api.main` so that deriving a key does not
import the whole web application — that import, made lazily from a service,
once bound a test's monkeypatched helper into a router for the rest of the
test run.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_session_secret(*, env_secret: str | None, secret_file: Path) -> str:
    """Resolve the SessionMiddleware HMAC key.

    Priority:
      1. VTS_SESSION_SECRET env (explicit / HA / multi-host deployments).
      2. Contents of secret_file. Auto-created on first start so a fresh
         self-hosted install does not require manual key generation.

    On first start the file is written with mode 0600 via O_EXCL so
    parallel uvicorn workers cannot both write — the loser of the race
    catches FileExistsError and reads what the winner wrote.
    """
    if env_secret:
        return env_secret

    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(secret_file), flags, 0o600)
    except FileExistsError:
        # Another worker won the race; read its value.
        return secret_file.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_secret)
    except Exception:
        # On any write failure, remove the half-written file so the next
        # start retries cleanly rather than reading an empty secret.
        try:
            secret_file.unlink()
        except OSError:
            pass
        raise
    logger.info(
        "generated new session secret at %s", secret_file
    )
    return new_secret
