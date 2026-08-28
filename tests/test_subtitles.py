"""Subtitle rendering (vts-fkyq / VOS-128): the raw transcript can be viewed as
subtitles instead of running text.

WebVTT is the chosen format because it carries speakers natively as voice tags
(`<v Name>`), which SRT has no field for. The renderer is pure and builds on the
same block structure the /player page already uses (`build_player_blocks`), so
one source of truth feeds both views.

The hard requirement (confirmed with the owner 2026-08-29): subtitles MUST work
WITHOUT speakers. Diarization is an option and most tasks have speaker=None —
that is the typical case, not an edge case.
"""
from __future__ import annotations

from vts.services.subtitles import format_timestamp, render_webvtt


def _block(start, end, text, label="", sentences=None):
    return {
        "start": start,
        "end": end,
        "text": text,
        "label": label,
        "sentences": sentences if sentences is not None else [
            {"start": start, "end": end, "text": text}
        ],
    }


def test_header_is_required_webvtt_magic():
    # A WebVTT file is invalid without this exact first line.
    out = render_webvtt([_block(0.0, 2.0, "hello")])
    assert out.startswith("WEBVTT\n")


def test_cue_carries_timing_and_text():
    out = render_webvtt([_block(1.5, 3.25, "hello there")])
    assert "00:00:01.500 --> 00:00:03.250" in out
    assert "hello there" in out


def test_undiarized_cue_has_no_voice_tag():
    # The typical case: no diarization -> speaker is None -> label is "".
    out = render_webvtt([_block(0.0, 2.0, "solo talking")])
    assert "<v" not in out
    assert "solo talking" in out


def test_diarized_cue_carries_voice_tag_with_resolved_name():
    out = render_webvtt([_block(0.0, 2.0, "hi", label="Alice")])
    assert "<v Alice>hi" in out


def test_sentences_become_separate_cues():
    # One block, three inner sentences -> three cues, each with its own timing,
    # so a subtitle line matches a sentence rather than a 5-minute chunk.
    block = _block(0.0, 9.0, "one two three", sentences=[
        {"start": 0.0, "end": 3.0, "text": "one"},
        {"start": 3.0, "end": 6.0, "text": "two"},
        {"start": 6.0, "end": 9.0, "text": "three"},
    ])
    out = render_webvtt([block])
    assert out.count("-->") == 3
    assert "00:00:00.000 --> 00:00:03.000" in out
    assert "00:00:06.000 --> 00:00:09.000" in out


def test_voice_tag_repeats_on_every_cue_of_a_block():
    # Each cue stands alone in a subtitle track — a viewer seeing only cue 2
    # must still know who is speaking.
    block = _block(0.0, 6.0, "a b", label="Bob", sentences=[
        {"start": 0.0, "end": 3.0, "text": "a"},
        {"start": 3.0, "end": 6.0, "text": "b"},
    ])
    out = render_webvtt([block])
    assert out.count("<v Bob>") == 2


def test_hours_are_rendered_in_full():
    # WebVTT timestamps are HH:MM:SS.mmm; a 2-hour recording must not wrap.
    out = render_webvtt([_block(7325.5, 7326.0, "late")])
    assert "02:02:05.500 --> 02:02:06.000" in out


def test_timestamp_formatting_is_zero_padded_milliseconds():
    assert format_timestamp(0.0) == "00:00:00.000"
    assert format_timestamp(61.007) == "00:01:01.007"
    assert format_timestamp(3600.0) == "01:00:00.000"


def test_negative_time_is_clamped_not_rendered_negative():
    # A malformed timing must not produce a cue the parser rejects.
    assert format_timestamp(-5.0) == "00:00:00.000"


def test_empty_text_cue_is_skipped():
    block = _block(0.0, 4.0, "kept", sentences=[
        {"start": 0.0, "end": 2.0, "text": "   "},
        {"start": 2.0, "end": 4.0, "text": "kept"},
    ])
    out = render_webvtt([block])
    assert out.count("-->") == 1
    assert "kept" in out


def test_no_blocks_still_yields_a_valid_empty_track():
    # Transcript not ready yet: a header-only file is valid WebVTT, an empty
    # string is not.
    out = render_webvtt([])
    assert out.strip() == "WEBVTT"


def test_cue_text_newlines_do_not_break_the_cue():
    # A blank line inside cue text would terminate the cue early and silently
    # truncate the track.
    block = _block(0.0, 2.0, "a\n\nb")
    out = render_webvtt([block])
    body = out.split("-->", 1)[1]
    assert "\n\n" not in body.strip()


def test_arrow_in_text_is_not_confused_with_a_timing_line():
    # Cue payloads may legitimately contain "-->"; it must not be emitted at the
    # start of a line where a parser would read it as a new cue timing.
    block = _block(0.0, 2.0, "-->")
    out = render_webvtt([block])
    cue_lines = [ln for ln in out.splitlines() if ln.strip() == "-->"]
    assert cue_lines == []


# ------------------------------------------------------------- subtitles API

import json
import uuid

import pytest

from tests.conftest import _TEST_USER_ID


async def _make_task(factory, tmp_path, entries):
    """A task whose outputs carry the given transcript entries."""
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    (outputs / "transcript.json").write_text(
        json.dumps({"text": " ".join(e["text"] for e in entries), "entries": entries}),
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
    return task_id


@pytest.mark.asyncio
async def test_subtitles_endpoint_serves_a_webvtt_track(authed_app, client, tmp_path):
    _app, factory = authed_app
    task_id = await _make_task(factory, tmp_path, [
        {"start": 0.0, "end": 1.5, "text": "Hello", "speaker": None},
        {"start": 1.5, "end": 3.0, "text": "world", "speaker": None},
    ])

    r = await client.get(f"/api/tasks/{task_id}/subtitles")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/vtt")
    body = r.text
    assert body.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in body
    assert "Hello" in body and "world" in body
    # Undiarized: no voice tags at all (the typical case).
    assert "<v" not in body


@pytest.mark.asyncio
async def test_subtitles_endpoint_uses_registry_names_for_speakers(
    authed_app, client, tmp_path
):
    _app, factory = authed_app
    task_id = await _make_task(factory, tmp_path, [
        {"start": 0.0, "end": 1.5, "text": "Hi", "speaker": "SPEAKER_00"},
    ])

    r = await client.get(f"/api/tasks/{task_id}/subtitles")
    assert r.status_code == 200, r.text
    # A diarized block carries a voice tag with the resolved display label,
    # never the raw SPEAKER_NN tag.
    assert "<v " in r.text
    assert "SPEAKER_00" not in r.text


@pytest.mark.asyncio
async def test_subtitles_endpoint_404s_for_another_users_task(
    authed_app, client, tmp_path
):
    _app, factory = authed_app
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    (outputs / "transcript.json").write_text(
        json.dumps({"text": "secret", "entries": [
            {"start": 0.0, "end": 1.0, "text": "secret", "speaker": None}
        ]}),
        encoding="utf-8",
    )
    other_user = uuid.uuid4()
    task_id = uuid.uuid4()
    async with factory() as session:
        from vts.db.models import User

        session.add(User(id=other_user, username="someone-else"))
        await session.flush()
        repo = Repo(session)
        await repo.create_task(
            user_id=other_user,
            source_url="https://example.com/v",
            options={"transcript": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    # Subtitles are a new way to read the transcript, not a new access model:
    # another user's task stays invisible.
    r = await client.get(f"/api/tasks/{task_id}/subtitles")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_subtitles_endpoint_returns_empty_track_when_not_ready(
    authed_app, client, tmp_path
):
    _app, factory = authed_app
    from vts.db.repo import Repo

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

    # No transcript yet -> a valid header-only track (200), matching how
    # /transcript-entries returns an empty block list rather than a 404.
    r = await client.get(f"/api/tasks/{task_id}/subtitles")
    assert r.status_code == 200, r.text
    assert r.text.strip() == "WEBVTT"
