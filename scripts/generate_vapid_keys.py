"""Generate a VAPID keypair for Web Push notifications.

Usage:
    python scripts/generate_vapid_keys.py

Prints the public/private keys as base64url (no padding). Paste the public
key into `vapid_public_key` and the private key into `vapid_private_key`
in your server config (or `VTS_VAPID_PUBLIC_KEY` / `VTS_VAPID_PRIVATE_KEY`).

The public key is also served to the browser via `/api/push/config` so the
frontend can subscribe.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid01


def main() -> None:
    v = Vapid01()
    v.generate_keys()
    pub_raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    priv_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()
    priv = base64.urlsafe_b64encode(priv_raw).rstrip(b"=").decode()
    print(f"vapid_public_key:  {pub}")
    print(f"vapid_private_key: {priv}")


if __name__ == "__main__":
    main()
