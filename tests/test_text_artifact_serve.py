"""Unit tests for _serve_text / _parse_range_header — the helpers that
make transcript/summary/redacted/log content negotiable for clients with
small response budgets (GPT Actions ~30KB)."""
from __future__ import annotations

import json
import uuid

import pytest
from starlette.requests import Request

from tests.conftest import _TEST_USER_ID
from vts.api.main import _MAX_TEXT_SLICE_CHARS, _parse_range_header, _serve_text


def _make_request(headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


# ---------------------------------------------------------------- range parser

@pytest.mark.parametrize("header,total,expected", [
    ("bytes=0-99", 1000, (0, 100)),
    ("bytes=100-199", 1000, (100, 100)),
    ("bytes=0-", 1000, (0, 1000)),       # open-ended end
    ("bytes=900-", 1000, (900, 100)),
    ("bytes=0-9999", 1000, (0, 1000)),   # clamps to total
])
def test_range_parser_valid(header, total, expected):
    assert _parse_range_header(header, total) == expected


@pytest.mark.parametrize("header,total", [
    ("", 100),
    ("garbage", 100),
    ("items=0-99", 100),         # wrong unit
    ("bytes=", 100),
    ("bytes=-50", 100),          # suffix-range not supported
    ("bytes=50", 100),           # missing dash
    ("bytes=50-30", 100),        # end before start
    ("bytes=200-300", 100),      # start past total
    ("bytes=0-99, 200-299", 1000),  # multipart
    ("bytes=abc-def", 100),
])
def test_range_parser_invalid_returns_none(header, total):
    assert _parse_range_header(header, total) is None


# ---------------------------------------------------------------- serve_text

def test_serve_text_default_returns_plain_text():
    r = _make_request()
    resp = _serve_text("hello world", "text/plain; charset=utf-8",
                       request=r, offset=None, limit=None)
    assert resp.status_code == 200
    assert resp.media_type.startswith("text/plain")
    assert resp.body == b"hello world"


def test_serve_text_accept_text_plain_wins():
    """text/plain explicit must NOT trigger JSON mode even if json also listed."""
    r = _make_request({"accept": "text/plain, application/json"})
    resp = _serve_text("hello", "text/plain; charset=utf-8",
                       request=r, offset=None, limit=None)
    assert resp.media_type.startswith("text/plain")
    assert resp.body == b"hello"


def test_serve_text_accept_json_returns_text_slice_out():
    r = _make_request({"accept": "application/json"})
    resp = _serve_text("abcdefghij", "text/plain; charset=utf-8",
                       request=r, offset=None, limit=None)
    assert resp.media_type == "application/json"
    payload = json.loads(resp.body)
    assert payload["text"] == "abcdefghij"
    assert payload["offset"] == 0
    assert payload["length"] == 10
    assert payload["total_length"] == 10
    assert payload["is_end"] is True


def test_serve_text_query_offset_triggers_json_mode():
    """Passing offset/limit without Accept: json still flips into JSON mode."""
    r = _make_request()  # no Accept header
    resp = _serve_text("abcdefghij", "text/plain; charset=utf-8",
                       request=r, offset=2, limit=4)
    assert resp.media_type == "application/json"
    payload = json.loads(resp.body)
    assert payload["text"] == "cdef"
    assert payload["offset"] == 2
    assert payload["length"] == 4
    assert payload["total_length"] == 10
    assert payload["is_end"] is False


def test_serve_text_is_end_true_when_slice_reaches_end():
    r = _make_request()
    resp = _serve_text("abcdefghij", "text/plain; charset=utf-8",
                       request=r, offset=5, limit=100)
    payload = json.loads(resp.body)
    assert payload["text"] == "fghij"
    assert payload["is_end"] is True


def test_serve_text_offset_past_total_returns_empty():
    r = _make_request({"accept": "application/json"})
    resp = _serve_text("abc", "text/plain; charset=utf-8",
                       request=r, offset=1000, limit=10)
    payload = json.loads(resp.body)
    assert payload["text"] == ""
    assert payload["offset"] == 3        # clamped to total
    assert payload["length"] == 0
    assert payload["is_end"] is True


def test_serve_text_range_header_returns_206():
    r = _make_request({"range": "bytes=2-5"})
    resp = _serve_text("abcdefghij", "text/plain; charset=utf-8",
                       request=r, offset=None, limit=None)
    assert resp.status_code == 206
    assert resp.body == b"cdef"
    assert resp.headers["content-range"] == "bytes 2-5/10"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.media_type.startswith("text/plain")


def test_serve_text_invalid_range_falls_back_to_full():
    r = _make_request({"range": "items=0-9"})  # wrong unit → ignored
    resp = _serve_text("abcdefghij", "text/plain; charset=utf-8",
                       request=r, offset=None, limit=None)
    assert resp.status_code == 200
    assert resp.body == b"abcdefghij"


def test_serve_text_safety_cap_on_slice_length():
    big = "x" * (_MAX_TEXT_SLICE_CHARS + 50)
    r = _make_request({"accept": "application/json"})
    resp = _serve_text(big, "text/plain; charset=utf-8",
                       request=r, offset=0, limit=10_000_000)
    payload = json.loads(resp.body)
    assert payload["length"] == _MAX_TEXT_SLICE_CHARS
    assert payload["is_end"] is False


# ---------------------------------------------------------- endpoint header

@pytest.mark.asyncio
async def test_transcript_endpoint_sends_no_cache(authed_app, client, tmp_path):
    """GET /api/tasks/{id}/transcript is served via _serve_text and must
    carry Cache-Control: no-cache — the transcript can be edited by a
    resolve-save (vts-552 re-render), so intermediate caches must not
    serve a stale body."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    transcript_file = tmp_path / "transcript.txt"
    transcript_file.write_text("hello world", encoding="utf-8")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"transcript": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        task.transcript_path = str(transcript_file)
        await session.commit()

    r = await client.get(f"/api/tasks/{task_id}/transcript")
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-cache"
