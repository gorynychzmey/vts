import json
import uuid

import pytest

from tests.conftest import _TEST_USER_ID


@pytest.mark.asyncio
async def test_speaker_crud_via_api(client):
    r = await client.post("/api/speakers", json={"name": "Вася"})
    assert r.status_code == 200
    sid = r.json()["id"]
    r = await client.get("/api/speakers")
    assert any(s["name"] == "Вася" for s in r.json())
    r = await client.patch(f"/api/speakers/{sid}", json={"name": "Василий"})
    assert r.json()["name"] == "Василий"
    r = await client.delete(f"/api/speakers/{sid}")
    assert r.status_code == 204
    r = await client.get("/api/speakers")
    assert all(s["name"] != "Василий" for s in r.json())


@pytest.mark.asyncio
async def test_delete_missing_speaker_404(client):
    r = await client.delete(f"/api/speakers/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rename_missing_speaker_404(client):
    r = await client.patch(f"/api/speakers/{uuid.uuid4()}", json={"name": "X"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_speaker_list_includes_sample_count(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        sp = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Alice")
        await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIO", audio_format="wav", duration_sec=3.0, source_task_id=None,
        )
        await session.commit()
        sid = str(sp.id)

    r = await client.get("/api/speakers")
    assert r.status_code == 200
    row = next(s for s in r.json() if s["id"] == sid)
    assert row["sample_count"] == 1


@pytest.mark.asyncio
async def test_list_and_delete_voice_samples(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        sp = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Alice")
        vs = await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIOBYTES", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        sid, vsid = str(sp.id), str(vs.id)

    r = await client.get(f"/api/speakers/{sid}/samples")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == vsid
    assert body[0]["duration_sec"] == 4.5
    assert body[0]["source_task_id"] is None
    assert "created_at" in body[0]

    r = await client.delete(f"/api/speakers/{sid}/samples/{vsid}")
    assert r.status_code == 204

    r = await client.delete(f"/api/speakers/{sid}/samples/{vsid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_voice_sample_rejects_mismatched_speaker_id(client, authed_app):
    """The {speaker_id} path segment must actually own {sample_id} — deleting
    sample S_A via a URL naming unrelated speaker B must 404 and leave S_A
    intact, even though S_A belongs to the same (authenticated) user."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        speaker_a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        speaker_b = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "B")
        sample_a = await repo.add_voice_sample(
            speaker_id=speaker_a.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIO_A", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        a_id, b_id, sa_id = str(speaker_a.id), str(speaker_b.id), str(sample_a.id)

    # Wrong speaker in the URL: must 404, and the sample must survive.
    r = await client.delete(f"/api/speakers/{b_id}/samples/{sa_id}")
    assert r.status_code == 404

    r = await client.get(f"/api/speakers/{a_id}/samples")
    assert r.status_code == 200
    assert any(s["id"] == sa_id for s in r.json())

    # Correct speaker in the URL: must succeed and actually delete it.
    r = await client.delete(f"/api/speakers/{a_id}/samples/{sa_id}")
    assert r.status_code == 204

    r = await client.get(f"/api/speakers/{a_id}/samples")
    assert r.status_code == 200
    assert all(s["id"] != sa_id for s in r.json())


@pytest.mark.asyncio
async def test_get_sample_audio(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        sp = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Alice")
        vs = await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"RIFF_AUDIO_BYTES", audio_format="wav", duration_sec=2.0, source_task_id=None,
        )
        await session.commit()
        vsid = str(vs.id)

    r = await client.get(f"/api/speakers/samples/{vsid}/audio")
    assert r.status_code == 200
    assert r.content == b"RIFF_AUDIO_BYTES"
    assert r.headers["content-type"] == "audio/wav"


@pytest.mark.asyncio
async def test_get_sample_audio_missing_404(client):
    r = await client.get(f"/api/speakers/samples/{uuid.uuid4()}/audio")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Resolution endpoint: POST /api/tasks/{task_id}/speakers
# ---------------------------------------------------------------------------


class _FakeDiarizationBackend:
    """Stub swapped in for the real diarization backend dependency.

    Returns a fixed embedding for any clip path so tests never touch a real
    sidecar. `embed_calls` records paths seen for assertions.
    """

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [0.2] * 256
        self.embed_calls: list[str] = []

    async def embed(self, audio_path) -> list[float]:
        self.embed_calls.append(str(audio_path))
        return self.vector


class _FakeRedis:
    async def publish(self, channel, message):
        return 0

    async def delete(self, key):
        # resolve_task_speakers' continue_task branch calls
        # bus.clear_pause_request (vts-80i resume-symmetry fix), which is a
        # bare redis DELETE — no pause key is ever set in this test's flow,
        # so a no-op stub is sufficient.
        return 0


def _override_diarization_backend(app, backend) -> None:
    from vts.api.deps import get_diarization_backend_dep

    async def _dep():
        return backend

    app.dependency_overrides[get_diarization_backend_dep] = _dep
    if not hasattr(app.state, "redis"):
        app.state.redis = _FakeRedis()


async def _seed_task_with_preview(factory, tmp_path, *, status="awaiting_input"):
    """Seed a task row + outputs/diarization.json + outputs/preview_*.wav,
    matching what DiarizeStep/MatchSpeakersStep would have produced."""
    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    diar = {"embedding_model": "ecapa", "embeddings": {"SPEAKER_00": [0.0] * 256}}
    (outputs / "diarization.json").write_text(json.dumps(diar), encoding="utf-8")

    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    (outputs / "speaker_previews.json").write_text(json.dumps(previews), encoding="utf-8")

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"diarize": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await repo.set_task_status(task, TaskStatus[status])
        await session.commit()
    return task_id, clip_path


@pytest.mark.asyncio
async def test_resolution_bind_new_creates_speaker_sample_and_decision(client, authed_app, tmp_path):
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    backend = _FakeDiarizationBackend()
    _override_diarization_backend(app, backend)

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "bind_new",
                    "new_name": "Вася",
                    "add_fragment": True,
                    "outcome": "manual_match",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text

    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        speakers = await repo.list_speakers(uuid.UUID(_TEST_USER_ID))
        assert any(s.name == "Вася" for s in speakers)
        sp = next(s for s in speakers if s.name == "Вася")
        samples = await repo.list_voice_samples(sp.id)
        assert len(samples) == 1
        assert samples[0].embedding_model == "ecapa"
        assert samples[0].source_task_id == task_id

    assert backend.embed_calls  # embed was actually invoked on the clip


@pytest.mark.asyncio
async def test_resolution_leaves_task_awaiting_when_not_continued(client, authed_app, tmp_path):
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "leave_anonymous",
                    "add_fragment": False,
                    "outcome": "left_anonymous",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text

    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(_TEST_USER_ID), task_id)
        assert task.status == TaskStatus.awaiting_input


@pytest.mark.asyncio
async def test_resolution_continue_task_resumes_to_queued(client, authed_app, tmp_path):
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "leave_anonymous",
                    "add_fragment": False,
                    "outcome": "left_anonymous",
                }
            ],
            "continue_task": True,
        },
    )
    assert r.status_code == 200, r.text

    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(_TEST_USER_ID), task_id)
        assert task.status == TaskStatus.queued


@pytest.mark.asyncio
async def test_resolution_unknown_task_404(client, authed_app):
    app, _factory = authed_app
    _override_diarization_backend(app, _FakeDiarizationBackend())
    r = await client.post(
        f"/api/tasks/{uuid.uuid4()}/speakers",
        json={"resolutions": [], "continue_task": False},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_resolution_is_transactional_all_or_nothing(client, authed_app, tmp_path):
    """Second resolution references a nonexistent speaker_id for bind_existing.
    The whole request must fail, and the first resolution's new speaker/sample
    must NOT have been persisted (no partial writes)."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    bogus_speaker_id = str(uuid.uuid4())

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "bind_new",
                    "new_name": "ShouldNotPersist",
                    "add_fragment": True,
                    "outcome": "manual_match",
                },
                {
                    "speaker_label": "SPEAKER_01",
                    "action": "bind_existing",
                    "speaker_id": bogus_speaker_id,
                    "add_fragment": False,
                    "outcome": "confirmed",
                },
            ],
            "continue_task": False,
        },
    )
    assert r.status_code in (400, 404, 422)

    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        speakers = await repo.list_speakers(uuid.UUID(_TEST_USER_ID))
        assert all(s.name != "ShouldNotPersist" for s in speakers)


async def _seed_task_without_diarization(factory, tmp_path, *, status="awaiting_input"):
    """Seed a task row with NO outputs/diarization.json (and no previews) —
    reproduces a malformed/incomplete awaiting_input task where embedding_model
    would resolve to "" in resolve_task_speakers."""
    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    # No diarization.json written. Still provide a preview so add_fragment
    # can reach the embedding_model check rather than failing earlier on a
    # missing-preview 422.
    clip_path = outputs / "preview_SPEAKER_00_0.wav"
    clip_path.write_bytes(b"RIFFCLIPBYTES")
    previews = {"SPEAKER_00": [{"path": str(clip_path), "start": 0.0, "end": 3.0}]}
    (outputs / "speaker_previews.json").write_text(json.dumps(previews), encoding="utf-8")

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"diarize": True},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await repo.set_task_status(task, TaskStatus[status])
        await session.commit()
    return task_id, clip_path


@pytest.mark.asyncio
async def test_resolution_add_fragment_without_embedding_model_422s(client, authed_app, tmp_path):
    """A resolution that ADDS A FRAGMENT on a task with no diarization.json
    (empty embedding_model) must 422 before any write, rather than silently
    persisting a fragment that can never match (embedding_model == "")."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_without_diarization(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        speaker = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "NoModel")
        await session.commit()
        speaker_id = str(speaker.id)

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "bind_existing",
                    "speaker_id": speaker_id,
                    "add_fragment": True,
                    "outcome": "manual_match",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 422

    async with factory() as session:
        repo = Repo(session)
        samples = await repo.list_voice_samples(uuid.UUID(speaker_id))
        assert samples == []


@pytest.mark.asyncio
async def test_resolution_leave_anonymous_succeeds_without_embedding_model(client, authed_app, tmp_path):
    """A resolution that does NOT add a fragment (leave_anonymous) must still
    succeed on a task with no diarization.json — the empty embedding_model
    guard must be scoped to add_fragment only."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_without_diarization(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "leave_anonymous",
                    "add_fragment": False,
                    "outcome": "left_anonymous",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_rebinding_speaker_label_deletes_prior_fragment_from_same_task(
    client, authed_app, tmp_path
):
    """Resolve task T binding SPEAKER_00 -> person A (adds fragment F_A with
    source_task_id=T). Re-resolve the SAME task, same label, to person B.
    F_A must be deleted (rolled back). A pre-existing fragment of A from a
    DIFFERENT task must survive untouched. A first-time binding must delete
    nothing."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    from vts.db.repo import Repo

    # Pre-existing fragment for person A from a different (unrelated) task —
    # must never be touched by the rollback.
    async with factory() as session:
        repo = Repo(session)
        person_a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        person_b = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "B")
        other_task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/other",
            options={},
            artifact_dir=str(tmp_path / "other_task"),
        )
        other_task_id = other_task.id
        old_sample = await repo.add_voice_sample(
            speaker_id=person_a.id, embedding=[0.05] * 256, embedding_model="ecapa",
            audio=b"OLD_AUDIO_A", audio_format="wav", duration_sec=2.0,
            source_task_id=other_task_id,
        )
        await session.commit()
        person_a_id, person_b_id, old_sample_id = str(person_a.id), str(person_b.id), old_sample.id

    # First-time resolution: bind SPEAKER_00 to person A, adding a fragment
    # from THIS task.
    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "bind_existing",
                    "speaker_id": person_a_id,
                    "add_fragment": True,
                    "outcome": "manual_match",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text

    async with factory() as session:
        repo = Repo(session)
        samples_a = await repo.list_voice_samples(uuid.UUID(person_a_id))
        # old sample (other task) + new sample (this task)
        assert len(samples_a) == 2
        new_sample = next(s for s in samples_a if s.id != old_sample_id)
        assert new_sample.source_task_id == task_id
        new_sample_id = new_sample.id
        # Nothing to roll back yet — no prior decision existed before this call.
        assert any(s.id == old_sample_id for s in samples_a)

    # Re-resolve the SAME task, SAME label, now to person B instead.
    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "bind_existing",
                    "speaker_id": person_b_id,
                    "add_fragment": True,
                    "outcome": "manual_match",
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text

    async with factory() as session:
        repo = Repo(session)
        samples_a_after = await repo.list_voice_samples(uuid.UUID(person_a_id))
        samples_b_after = await repo.list_voice_samples(uuid.UUID(person_b_id))

        # The fragment this task added to A is gone.
        assert all(s.id != new_sample_id for s in samples_a_after)
        # The pre-existing fragment from a different task is untouched.
        assert any(s.id == old_sample_id for s in samples_a_after)
        assert len(samples_a_after) == 1

        # B now has exactly one fragment, from this task.
        assert len(samples_b_after) == 1
        assert samples_b_after[0].source_task_id == task_id
