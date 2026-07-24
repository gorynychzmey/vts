"""The /player/{task_id} page renders the media element PLUS a clickable
transcript (VOS-111 / vts-at8). Clicking a phrase seeks the player to that
phrase's start time.

Two layers are tested:
  * `_player_page_html` — the pure renderer: given a title, a media tag and
    transcript entries, it emits self-contained HTML with the entries as
    clickable, timecode-bearing elements and the seek script.
  * GET /player/{id} end-to-end through the authed client — seeds a task with
    a real media file and an outputs/transcript.json, and asserts the page
    carries the transcript.
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.conftest import _TEST_USER_ID
from vts.api.main import _player_page_html


# --------------------------------------------------------------- pure renderer

def test_player_html_renders_clickable_entries_with_start_times():
    entries = [
        {"start": 0.0, "end": 2.5, "text": "First phrase", "speaker": "SPEAKER_00"},
        {"start": 2.5, "end": 5.0, "text": "Second phrase", "speaker": "SPEAKER_01"},
    ]
    html = _player_page_html(
        title="My video",
        media_tag='<video controls src="/api/tasks/x/media"></video>',
        entries=entries,
    )
    # The media element is present.
    assert "<video" in html
    # Both phrases render as text.
    assert "First phrase" in html
    assert "Second phrase" in html
    # Each phrase carries its start time in a data attribute the seek script reads.
    assert 'data-start="0.0"' in html or 'data-start="0"' in html
    assert 'data-start="2.5"' in html
    # A seek script wires clicks to currentTime.
    assert "currentTime" in html


def test_player_html_escapes_entry_text():
    entries = [{"start": 0.0, "end": 1.0, "text": "<script>alert(1)</script>", "speaker": ""}]
    html = _player_page_html(
        title="t",
        media_tag="<audio></audio>",
        entries=entries,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_player_html_without_entries_still_renders_media():
    html = _player_page_html(title="t", media_tag="<audio></audio>", entries=[])
    assert "<audio></audio>" in html


def test_player_html_live_subscribes_to_events_for_its_task():
    """When a task_id is given, the page opens an SSE connection to /api/events,
    knows its own task_id (to filter events), and reacts to transcript_updated
    (re-fetch entries) and task_status (media-gone) — plus a media error handler."""
    tid = "11111111-1111-1111-1111-111111111111"
    html = _player_page_html(
        title="t",
        media_tag='<video src="/api/tasks/x/media"></video>',
        entries=[{"start": 0.0, "end": 1.0, "text": "hi", "speaker": ""}],
        task_id=tid,
    )
    # Opens the shared SSE stream.
    assert "EventSource" in html
    assert "/api/events" in html
    # Knows its own id so it can ignore other tasks' events.
    assert tid in html
    # Reacts to the universal transcript signal and re-fetches entries.
    assert "transcript_updated" in html
    assert "transcript-entries" in html
    # Reacts to task deletion/cancel and to a media load error.
    assert "task_status" in html
    assert '"error"' in html or "'error'" in html or "addEventListener(\"error\"" in html


def test_player_html_no_task_id_omits_live_subscription():
    """Without a task_id (pure-render callers / tests), no SSE wiring is emitted."""
    html = _player_page_html(
        title="t",
        media_tag="<audio></audio>",
        entries=[],
    )
    assert "EventSource" not in html


def test_player_html_media_unavailable_shows_message_not_media():
    """media_tag=None → a human-readable 'media unavailable' page, no <video>/
    <audio>. Used when the media file is gone (TTL / archive / delete)."""
    html = _player_page_html(title="t", media_tag=None, entries=[])
    assert "<video" not in html
    assert "<audio" not in html
    # A stable marker the page + tests key on, locale-independent.
    assert 'data-media-unavailable' in html
    # Both locale messages are embedded so the page can pick client-side.
    assert "no longer available" in html.lower()
    assert "более не доступно" in html.lower()


# ------------------------------------------------------------------ end-to-end

@pytest.mark.asyncio
async def test_player_endpoint_includes_transcript(authed_app, client, tmp_path):
    _app, factory = authed_app
    from vts.db.repo import Repo

    # Seed a real media file where _find_media_file looks: <artifact>/media/.
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "audio.original.mp3").write_bytes(b"\x00\x00")

    # Seed the transcript entries where the endpoint reads them: outputs/.
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    (outputs_dir / "transcript.json").write_text(
        json.dumps(
            {
                "text": "Hello there world",
                "entries": [
                    {"start": 0.0, "end": 1.5, "text": "Hello there", "speaker": "SPEAKER_00"},
                    {"start": 1.5, "end": 3.0, "text": "world", "speaker": "SPEAKER_00"},
                ],
            }
        ),
        encoding="utf-8",
    )

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"transcript": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    r = await client.get(f"/player/{task_id}")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Hello there" in body
    assert "world" in body
    assert 'data-start="1.5"' in body
    assert "currentTime" in body


@pytest.mark.asyncio
async def test_player_endpoint_wires_live_sse_with_task_id(authed_app, client, tmp_path):
    """The served /player page carries the live SSE wiring keyed on the real
    task id, so transcript_updated / task_status events drive it."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "audio.original.mp3").write_bytes(b"\x00\x00")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"transcript": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    r = await client.get(f"/player/{task_id}")
    assert r.status_code == 200, r.text
    assert "EventSource" in r.text
    assert str(task_id) in r.text
    assert "transcript_updated" in r.text


@pytest.mark.asyncio
async def test_player_endpoint_media_gone_returns_200_message(authed_app, client, tmp_path):
    """Opening /player when the media file is gone (but the task exists) must
    render a human-readable 'media unavailable' page with 200, not a raw 404."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    # No media/ dir at all — _find_media_file returns None.
    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"transcript": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    r = await client.get(f"/player/{task_id}")
    assert r.status_code == 200, r.text
    assert "data-media-unavailable" in r.text
    assert "<video" not in r.text and "<audio" not in r.text
