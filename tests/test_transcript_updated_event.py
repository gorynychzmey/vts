"""transcript_updated is the universal "transcript is whole again" signal
(vts-at8 / VOS-111). It fires when merge_transcript finishes AND after
rerender_transcript on speaker resolve/save, so both the /player page and the
main SPA can re-fetch the transcript live rather than each owning ad-hoc
refresh logic.

Also covers GET /api/tasks/{id}/transcript-entries, the JSON endpoint the
/player page pulls when it receives the event.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import _TEST_USER_ID


# ------------------------------------------------------------ merge-step event

class _RecordingBus:
    """Captures every publish_event call so a test can assert what fired."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_event(self, **kwargs: object) -> None:
        self.events.append(dict(kwargs))


def test_merge_transcript_step_publishes_transcript_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reuse the sibling test module's fakes to drive a real MergeTranscriptStep.run.
    from tests.test_merge_transcript_step import _segments_with_one_silent_chunk, _ctx, _dirs
    from vts.pipeline.steps.base import StepState
    from vts.pipeline.steps.transcription import MergeTranscriptStep

    dirs = _dirs(tmp_path)
    segments = _segments_with_one_silent_chunk()
    ctx = _ctx(monkeypatch, segments)
    bus = _RecordingBus()
    ctx.bus = bus

    st = StepState(
        task_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test_transcript_updated_event"),
        task_options={"language": "ru"},
    )

    assert asyncio.run(MergeTranscriptStep().run(ctx, st)) is True

    events = [e for e in bus.events if e.get("event") == "transcript_updated"]
    assert events, f"expected a transcript_updated event, got {[e.get('event') for e in bus.events]}"
    assert str(events[0]["task_id"]) == str(st.task_id)


# ------------------------------------------------------- transcript-entries API

@pytest.mark.asyncio
async def test_transcript_entries_endpoint_returns_entries(authed_app, client, tmp_path):
    _app, factory = authed_app
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "transcript.json").write_text(
        json.dumps(
            {
                "text": "Hello world",
                "entries": [
                    {"start": 0.0, "end": 1.5, "text": "Hello", "speaker": "SPEAKER_00"},
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

    r = await client.get(f"/api/tasks/{task_id}/transcript-entries")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert [e["text"] for e in payload["entries"]] == ["Hello", "world"]
    assert payload["entries"][1]["start"] == 1.5


class _SerializingFakeRedis:
    """Fake Redis that JSON-serializes on publish exactly as prod's RedisBus
    does — so a UUID slipped into the event payload raises here, the same way
    it would in production. Captures published messages for assertion."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.published: list[dict] = []

    async def publish(self, channel, message) -> int:
        # RedisBus already json.dumps()'d `message`; parse it back so the test
        # both proves it was serializable AND can inspect it.
        self.published.append(json.loads(message))
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value

    async def set(self, key, value, **kwargs) -> None:
        self.store[key] = value

    async def delete(self, *keys) -> None:
        for k in keys:
            self.store.pop(k, None)


@pytest.mark.asyncio
async def test_resolve_save_publishes_serializable_transcript_updated(
    authed_app, client, tmp_path, monkeypatch
):
    """Resolve/save fires transcript_updated with a JSON-serializable, string
    user_id targeting the task owner (so an impersonating admin's save still
    reaches the owner's open /player). A raw UUID user_id would raise in
    RedisBus.publish_event's json.dumps — this test's redis serializes for real."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "transcript.json").write_text(
        json.dumps(
            {
                "text": "hi",
                "entries": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": "SPEAKER_00"}],
            }
        ),
        encoding="utf-8",
    )
    # can_resolve_speakers_task keys off this file's presence (diarized path).
    (outputs / "speaker_matches.json").write_text("{}", encoding="utf-8")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"transcript": True, "diarize": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    fake_redis = _SerializingFakeRedis()
    _app.state.redis = fake_redis

    # Empty resolutions: no speaker changes needed to reach the rerender +
    # event publish path; the endpoint still re-renders and notifies.
    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={"resolutions": [], "continue_task": False},
    )
    assert r.status_code == 200, r.text

    updates = [m for m in fake_redis.published if m.get("event") == "transcript_updated"]
    assert updates, f"no transcript_updated published; saw {[m.get('event') for m in fake_redis.published]}"
    msg = updates[0]
    assert msg["user_id"] == _TEST_USER_ID  # owner id, as a string
    assert msg["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_transcript_entries_endpoint_empty_when_no_transcript(authed_app, client, tmp_path):
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

    r = await client.get(f"/api/tasks/{task_id}/transcript-entries")
    assert r.status_code == 200, r.text
    assert r.json()["entries"] == []
