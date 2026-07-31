from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest

from vts.mcp.cursor import encode_cursor, decode_cursor


def test_round_trip_preserves_created_at_and_id():
    ts = datetime(2026, 7, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)
    tid = uuid.uuid4()
    token = encode_cursor(ts, tid)
    assert isinstance(token, str) and token
    got_ts, got_id = decode_cursor(token)
    assert got_ts == ts
    assert got_id == tid


def test_round_trip_zero_microseconds():
    ts = datetime(2026, 7, 30, 12, 34, 56, 0, tzinfo=timezone.utc)
    tid = uuid.uuid4()
    got_ts, got_id = decode_cursor(encode_cursor(ts, tid))
    assert got_ts == ts and got_id == tid


@pytest.mark.parametrize("bad", [
    "not-base64-!!!",
    "",
    "YWJjZGVm",            # valid base64 but no '|' separator
])
def test_decode_rejects_malformed(bad):
    with pytest.raises(ValueError):
        decode_cursor(bad)


def test_decode_rejects_bad_datetime_and_uuid():
    raw = base64.urlsafe_b64encode(b"not-a-date|not-a-uuid").decode().rstrip("=")
    with pytest.raises(ValueError):
        decode_cursor(raw)
