from __future__ import annotations

import re


LIVE_NOT_STARTED_PATTERNS = (
    re.compile(r"this live event will begin in a few moments", re.IGNORECASE),
    re.compile(r"this live event has not started", re.IGNORECASE),
    re.compile(r"premieres in", re.IGNORECASE),
)


def classify_failure_code(error_message: str | None) -> str | None:
    message = (error_message or "").strip()
    if not message:
        return None
    for pattern in LIVE_NOT_STARTED_PATTERNS:
        if pattern.search(message):
            return "download_live_not_started"
    return None

