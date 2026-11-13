"""Signed media links for the MCP player view (vts-vtma.3).

The view plays media from the host's sandboxed iframe, with no session, so
the link has to authorise itself. These tests pin what it must refuse: an
expired link, a tampered one, one for another recording or user, and one
that claims an expiry this server never issues.
"""
from __future__ import annotations

import time
import uuid

import pytest

from tests.conftest import _TEST_USER_ID
from vts.services.media_urls import (
    TTL_SECONDS,
    signed_media_path,
    verify_media_signature,
)

_KEY = b"k" * 32
_OWNER = uuid.UUID(_TEST_USER_ID)


# ------------------------------------------------------------ the signature

def test_a_fresh_link_verifies():
    rec = uuid.uuid4()
    path, exp = signed_media_path(_KEY, rec, _OWNER, now=1000)
    sig = path.rsplit("sig=", 1)[1]
    assert exp == 1000 + TTL_SECONDS
    assert verify_media_signature(_KEY, rec, _OWNER, exp, sig, now=1000)


@pytest.mark.parametrize("case", ["expired", "other recording", "other user", "other key", "tampered sig", "far future"])
def test_what_a_link_must_not_open(case):
    rec = uuid.uuid4()
    path, exp = signed_media_path(_KEY, rec, _OWNER, now=1000)
    sig = path.rsplit("sig=", 1)[1]
    args = dict(key=_KEY, recording_id=rec, user_id=_OWNER, exp=exp, sig=sig, now=1000)
    if case == "expired":
        args["now"] = exp + 1
    elif case == "other recording":
        args["recording_id"] = uuid.uuid4()
    elif case == "other user":
        args["user_id"] = uuid.uuid4()
    elif case == "other key":
        args["key"] = b"x" * 32
    elif case == "tampered sig":
        args["sig"] = ("0" if sig[0] != "0" else "1") + sig[1:]
    elif case == "far future":
        # Correctly signed, but for an expiry no link from here ever carries.
        far = 1000 + 30 * 24 * 3600
        far_path, _ = signed_media_path(_KEY, rec, _OWNER, now=far - TTL_SECONDS)
        args["exp"] = far
        args["sig"] = far_path.rsplit("sig=", 1)[1]
    assert not verify_media_signature(
        args["key"], args["recording_id"], args["user_id"], args["exp"], args["sig"], now=args["now"]
    )


# ------------------------------------------------------------ the route

@pytest.fixture
async def media_app(authed_app, tmp_path, monkeypatch):
    """The app with a fixed signing key and one recording that has media."""
    from vts.api.deps import get_settings_dep
    from vts.db.repo import Repo

    app, factory = authed_app
    settings = get_settings_dep().model_copy(update={"session_secret": "test-session-secret"})
    app.dependency_overrides[get_settings_dep] = lambda: settings

    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "audio.original.mp3").write_bytes(bytes(range(256)) * 4)
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_OWNER, source_url="file://clip.mp3", options={}, artifact_dir=str(tmp_path),
        )
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
    from vts.services.media_urls import media_signing_key

    return app, recording, media_signing_key(settings)


async def test_a_signed_link_plays_and_seeks(media_app, client):
    _app, recording, key = media_app
    path, _ = signed_media_path(key, recording.id, _OWNER)
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")
    assert len(r.content) == 1024
    # Seeking needs Range; without it a player cannot jump into the file.
    r = await client.get(path, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == bytes(range(10, 20))


async def test_without_a_signature_nothing_is_served(media_app, client):
    _app, recording, _key = media_app
    r = await client.get(f"/api/recordings/{recording.id}/media")
    assert r.status_code in (404, 422)
    assert not r.content.startswith(bytes(range(16)))


@pytest.mark.parametrize("case", ["expired", "tampered", "foreign user"])
async def test_a_bad_link_reads_as_missing(media_app, client, case):
    """Every refusal is the same 404, so a probe learns nothing."""
    _app, recording, key = media_app
    if case == "expired":
        path, _ = signed_media_path(key, recording.id, _OWNER, now=time.time() - 2 * TTL_SECONDS)
    elif case == "tampered":
        path, _ = signed_media_path(key, recording.id, _OWNER)
        path = path[:-1] + ("0" if path[-1] != "0" else "1")
    else:
        # A valid signature for someone who does not own the recording.
        path, _ = signed_media_path(key, recording.id, uuid.uuid4())
    r = await client.get(path)
    assert r.status_code == 404
    assert r.json()["detail"] == "Media not available"


async def test_a_recording_without_media_reads_as_missing(media_app, client, tmp_path):
    _app, recording, key = media_app
    (tmp_path / "media" / "audio.original.mp3").unlink()
    path, _ = signed_media_path(key, recording.id, _OWNER)
    r = await client.get(path)
    assert r.status_code == 404
