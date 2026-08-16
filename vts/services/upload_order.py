"""Decide the order in which uploaded files are concatenated (vts-vm0).

The order files are joined in is the order they are heard in, and it is decided
server-side with no user step — so the fallback chain matters. Preference:

  1. container `creation_time` — the true recording time, survives copying
  2. the browser's `lastModified` — present for every file, but for a file
     downloaded from a messenger it is the DOWNLOAD time
  3. natural filename sort — digit runs compared numerically

Step 3 is load-bearing rather than a formality: ogg/opus/wav and
avi/wmv/ts carry no `creation_time` at all (measured — see the spec), and opus
is exactly what Telegram and WhatsApp voice messages use.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """Sort key where digit runs compare numerically: rec_9 before rec_10."""
    return [int(part) if part.isdigit() else part.lower() for part in _DIGITS.split(name)]


def _parse(value: str | None) -> datetime | None:
    """Parse a container creation_time tag into a COMPARABLE datetime.

    Always tz-aware on the way out. The tag comes straight from ffprobe and its
    shape follows the container, not our code: MP4/MOV write a trailing "Z",
    while Matroska and friends can write "YYYY-MM-DD HH:MM:SS" with no
    designator. Mixing those two in one sorted() raises "can't compare
    offset-naive and offset-aware datetimes" — inside finalize, which loses the
    upload (vts-5z9k).

    A naive tag is read as UTC. That is what the aware ones almost always say
    anyway, and being an hour or two out only matters if it reorders the set —
    in which case the alternative was a crash, not a better order.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_order(entries: list[dict]) -> tuple[list[dict], str]:
    """Return (ordered entries, which rule decided the order).

    A signal is used only when it is present AND discriminating for every file:
    a set downloaded in one go shares one mtime, which is no order at all, and
    silently trusting it would scramble the recording.
    """
    items = list(entries)

    stamps = [_parse(e.get("creation_time")) for e in items]
    if all(s is not None for s in stamps) and len({s for s in stamps} ) == len(items):
        return [e for _, e in sorted(zip(stamps, items), key=lambda pair: pair[0])], "creation_time"

    mtimes = [e.get("last_modified") for e in items]
    if all(m is not None for m in mtimes) and len(set(mtimes)) == len(items):
        return [e for _, e in sorted(zip(mtimes, items), key=lambda pair: pair[0])], "last_modified"

    return sorted(items, key=lambda e: natural_key(e.get("filename", ""))), "filename"
