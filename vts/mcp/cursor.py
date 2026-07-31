from __future__ import annotations

import base64
import uuid
from datetime import datetime

# Opaque pagination cursor for the MCP list_tasks tool. Encodes the composite
# (created_at, id) key that Repo.list_tasks_page pages on. Clients treat the
# string as opaque — they echo back the next_cursor from the previous page.

_SEP = "|"


def encode_cursor(created_at: datetime, task_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}{_SEP}{task_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    """Decode an opaque cursor to ``(created_at, task_id)``.

    Raises ``ValueError`` on any malformation (bad base64, missing separator,
    unparseable datetime or uuid).
    """
    if not raw:
        raise ValueError("empty cursor")
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
    except Exception as exc:  # binascii.Error, UnicodeDecodeError
        raise ValueError("cursor is not valid base64") from exc
    if _SEP not in decoded:
        raise ValueError("cursor missing separator")
    ts_str, _, id_str = decoded.partition(_SEP)
    try:
        created_at = datetime.fromisoformat(ts_str)
    except ValueError as exc:
        raise ValueError("cursor has invalid datetime") from exc
    try:
        task_id = uuid.UUID(id_str)
    except ValueError as exc:
        raise ValueError("cursor has invalid uuid") from exc
    return created_at, task_id
