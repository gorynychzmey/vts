"""Tests for GET /api/tasks/{task_id}/speaker-previews/{speaker_label}/{index}/audio

Covers: happy path (bytes + content-type), 404 for bad label / bad index /
missing speaker_previews.json, 404 for a task owned by another user (user
isolation), and the path-traversal guard (a speaker_previews.json entry
pointing outside the task's outputs dir must be refused, not served).
"""
import json
import uuid

import pytest

from tests.conftest import _TEST_USER_ID

_OTHER_USER_ID = "00000000-0000-0000-0000-0000000000b2"


async def _seed_task_with_previews(factory, tmp_path, previews: dict, *, user_id=_TEST_USER_ID):
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(user_id),
            source_url="https://example.com/v",
            options={"diarize": True},
            artifact_dir=str(tmp_path),
        )
        await session.commit()
        task_id = task.id

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "speaker_previews.json").write_text(json.dumps(previews), encoding="utf-8")
    return task_id


async def _seed_task_with_raw_previews_bytes(factory, tmp_path, raw: bytes, *, user_id=_TEST_USER_ID):
    """Like _seed_task_with_previews but writes arbitrary bytes verbatim,
    for exercising malformed/non-dict speaker_previews.json content."""
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(user_id),
            source_url="https://example.com/v",
            options={"diarize": True},
            artifact_dir=str(tmp_path),
        )
        await session.commit()
        task_id = task.id

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "speaker_previews.json").write_bytes(raw)
    return task_id


@pytest.mark.asyncio
async def test_get_speaker_preview_audio_200(client, authed_app, tmp_path):
    _app, factory = authed_app
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    task_id = await _seed_task_with_previews(factory, tmp_path, previews)

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code == 200
    assert r.content == b"RIFFCLIPBYTES"
    assert r.headers["content-type"] == "audio/wav"


@pytest.mark.asyncio
async def test_get_speaker_preview_missing_json_404(client, authed_app, tmp_path):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={},
            artifact_dir=str(tmp_path),
        )
        await session.commit()
        task_id = task.id

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_speaker_preview_bad_label_404(client, authed_app, tmp_path):
    _app, factory = authed_app
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    task_id = await _seed_task_with_previews(factory, tmp_path, previews)

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_99/0/audio")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_speaker_preview_bad_index_404(client, authed_app, tmp_path):
    _app, factory = authed_app
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    task_id = await _seed_task_with_previews(factory, tmp_path, previews)

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/7/audio")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_speaker_preview_other_users_task_404(client, authed_app, tmp_path):
    """User isolation: a task owned by a different user must 404, never serve
    audio, regardless of what its speaker_previews.json contains."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    # Seed the "other" user row so the FK on tasks.user_id is satisfiable.
    from vts.db.models import User

    async with factory() as session:
        session.add(User(id=uuid.UUID(_OTHER_USER_ID), username="other"))
        await session.commit()

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    task_id = await _seed_task_with_previews(factory, tmp_path, previews, user_id=_OTHER_USER_ID)

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_speaker_preview_path_traversal_refused(client, authed_app, tmp_path):
    """If speaker_previews.json (tampered, or via a bug) points OUTSIDE the
    task's outputs dir, the route must refuse to serve it - not follow the
    path blindly. This is the defense against a tampered json AND against
    path-traversal via speaker_label/index (which this test doesn't need to
    craft directly, since the guard operates on the resolved json path)."""
    _app, factory = authed_app
    outside_target = tmp_path.parent / "outside_secret.txt"
    outside_target.write_text("SECRET_SHOULD_NOT_BE_SERVED", encoding="utf-8")

    previews = {"SPEAKER_00": [{"path": str(outside_target), "start": 0.0, "end": 3.0}]}
    task_id = await _seed_task_with_previews(factory, tmp_path, previews)

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code in (403, 404)
    assert r.content != b"SECRET_SHOULD_NOT_BE_SERVED"


@pytest.mark.asyncio
async def test_get_speaker_preview_malformed_json_404(client, authed_app, tmp_path):
    """A corrupt speaker_previews.json (invalid JSON) must 404, not 500."""
    _app, factory = authed_app
    task_id = await _seed_task_with_raw_previews_bytes(factory, tmp_path, b"{not json")

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_speaker_preview_non_dict_json_404(client, authed_app, tmp_path):
    """Valid JSON that isn't a dict (e.g. a bare list) must 404, not 500."""
    _app, factory = authed_app
    task_id = await _seed_task_with_raw_previews_bytes(factory, tmp_path, b"[]")

    r = await client.get(f"/api/tasks/{task_id}/speaker-previews/SPEAKER_00/0/audio")
    assert r.status_code == 404
