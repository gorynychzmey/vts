from __future__ import annotations

import json

from cryptography.fernet import Fernet


class SecretsKeyMissing(RuntimeError):
    """VTS_SECRETS_KEY is not configured but a secret operation was requested."""


def load_secrets_key(settings) -> str:
    key = getattr(settings, "secrets_key", "") or ""
    if not key.strip():
        raise SecretsKeyMissing(
            "VTS_SECRETS_KEY is not set; delivery targets with secrets are unavailable"
        )
    return key


def encrypt_secrets(data: dict[str, str], key: str) -> bytes:
    payload = json.dumps(data, ensure_ascii=True).encode("utf-8")
    return Fernet(key.encode("utf-8")).encrypt(payload)


def decrypt_secrets(blob: bytes, key: str) -> dict[str, str]:
    raw = Fernet(key.encode("utf-8")).decrypt(bytes(blob))
    return json.loads(raw.decode("utf-8"))
