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
    matching what DiarizeStep/MatchSpeakersStep would have produced.

    Writes speaker_matches.json too: `can_resolve_speakers_task` (vts-552) gates
    the resolve endpoint on that artifact existing (the file only the diarized
    match_speakers path writes), and every resolve test in this module exercises
    a task past that point. A completed `match_speakers` step is still seeded to
    mirror a real diarized task, but the file — not the step status — is the gate.
    """
    from vts.db.models import StepStatus, TaskStatus
    from vts.db.repo import Repo

    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    diar = {"embedding_model": "ecapa", "embeddings": {"SPEAKER_00": [0.0] * 256}}
    (outputs / "diarization.json").write_text(json.dumps(diar), encoding="utf-8")

    matches = {"SPEAKER_00": {"outcome": "candidate", "speaker_id": None, "candidates": []}}
    (outputs / "speaker_matches.json").write_text(json.dumps(matches), encoding="utf-8")

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
        step = await repo.upsert_step(task_id, "match_speakers")
        await repo.set_step_status(step, StepStatus.completed)
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
    would resolve to "" in resolve_task_speakers.

    Still writes speaker_matches.json (see `_seed_task_with_preview`) so
    `can_resolve_speakers_task` (vts-552) admits it — this helper is about a
    missing diarization.json, not an unresolved capability gate.
    """
    from vts.db.models import StepStatus, TaskStatus
    from vts.db.repo import Repo

    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    # No diarization.json written. speaker_matches.json IS written — it is the
    # capability gate, and its presence without diarization.json is exactly the
    # "embedding_model resolves to ''" case this helper reproduces.
    matches = {"SPEAKER_00": {"outcome": "candidate", "speaker_id": None, "candidates": []}}
    (outputs / "speaker_matches.json").write_text(json.dumps(matches), encoding="utf-8")

    # Still provide a preview so add_fragment can reach the embedding_model check
    # rather than failing earlier on a missing-preview 422.
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
        step = await repo.upsert_step(task_id, "match_speakers")
        await repo.set_step_status(step, StepStatus.completed)
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


@pytest.mark.asyncio
async def test_move_sample_via_api(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        b = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "B")
        sample = await repo.add_voice_sample(
            speaker_id=a.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIO", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        a_id, b_id, s_id = str(a.id), str(b.id), str(sample.id)

    r = await client.post(
        f"/api/speakers/{a_id}/samples/{s_id}/move", json={"target_speaker_id": b_id}
    )
    assert r.status_code == 200
    assert r.json()["id"] == s_id

    assert (await client.get(f"/api/speakers/{a_id}/samples")).json() == []
    moved = (await client.get(f"/api/speakers/{b_id}/samples")).json()
    assert [s["id"] for s in moved] == [s_id]


@pytest.mark.asyncio
async def test_move_sample_rejects_mismatched_speaker_id(client, authed_app):
    """The {speaker_id} path segment must own {sample_id}, as for delete."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        b = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "B")
        sample = await repo.add_voice_sample(
            speaker_id=a.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIO", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        a_id, b_id, s_id = str(a.id), str(b.id), str(sample.id)

    # sample belongs to A, but the URL names B
    r = await client.post(
        f"/api/speakers/{b_id}/samples/{s_id}/move", json={"target_speaker_id": b_id}
    )
    assert r.status_code == 404
    assert len((await client.get(f"/api/speakers/{a_id}/samples")).json()) == 1


@pytest.mark.asyncio
async def test_move_sample_unknown_target_404(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        sample = await repo.add_voice_sample(
            speaker_id=a.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"AUDIO", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        a_id, s_id = str(a.id), str(sample.id)

    r = await client.post(
        f"/api/speakers/{a_id}/samples/{s_id}/move",
        json={"target_speaker_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_merge_via_api(client):
    a = (await client.post("/api/speakers", json={"name": "Вася-1"})).json()
    b = (await client.post("/api/speakers", json={"name": "Вася-2"})).json()

    r = await client.post(f"/api/speakers/{a['id']}/merge", json={"target_id": b["id"]})
    assert r.status_code == 204

    names = [s["name"] for s in (await client.get("/api/speakers")).json()]
    assert "Вася-1" not in names
    assert "Вася-2" in names


@pytest.mark.asyncio
async def test_merge_same_speaker_409(client):
    a = (await client.post("/api/speakers", json={"name": "A"})).json()
    r = await client.post(f"/api/speakers/{a['id']}/merge", json={"target_id": a["id"]})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_merge_unknown_target_404(client):
    a = (await client.post("/api/speakers", json={"name": "A"})).json()
    r = await client.post(
        f"/api/speakers/{a['id']}/merge", json={"target_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_move_candidates_endpoint(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    def _vec(first: float) -> list[float]:
        v = [0.0] * 256
        v[0] = first
        v[1] = 1.0
        return v

    async with factory() as session:
        repo = Repo(session)
        owner = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Владелец")
        near = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Близкий")
        far = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Далёкий")
        sample = await repo.add_voice_sample(
            speaker_id=owner.id, embedding=_vec(1.0), embedding_model="m",
            audio=b"A", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await repo.add_voice_sample(
            speaker_id=near.id, embedding=_vec(0.95), embedding_model="m",
            audio=b"B", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await repo.add_voice_sample(
            speaker_id=far.id, embedding=_vec(-1.0), embedding_model="m",
            audio=b"C", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        owner_id, s_id = str(owner.id), str(sample.id)

    r = await client.get(f"/api/speakers/{owner_id}/samples/{s_id}/move-candidates")
    assert r.status_code == 200
    body = r.json()
    assert [c["name"] for c in body] == ["Близкий", "Далёкий"]
    # the fragment's own owner is never a destination
    assert all(c["id"] != owner_id for c in body)
    assert body[0]["distance"] is not None


@pytest.mark.asyncio
async def test_move_candidates_rejects_mismatched_speaker_id(client, authed_app):
    _app, factory = authed_app
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        a = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "A")
        b = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "B")
        sample = await repo.add_voice_sample(
            speaker_id=a.id, embedding=[0.1] * 256, embedding_model="m",
            audio=b"A", audio_format="wav", duration_sec=4.5, source_task_id=None,
        )
        await session.commit()
        b_id, s_id = str(b.id), str(sample.id)

    r = await client.get(f"/api/speakers/{b_id}/samples/{s_id}/move-candidates")
    assert r.status_code == 404


def _task_with_matches(tmp_path, *, status, write_matches):
    """A Task whose artifact_dir optionally holds speaker_matches.json.

    can_resolve_speakers_task keys off the PRESENCE of that artifact (the file
    the diarized match_speakers path writes), not the step's status — a
    non-diarized task's match_speakers still completes but writes no file.
    """
    from vts.db.models import Task
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    if write_matches:
        (outputs / "speaker_matches.json").write_text("{}", encoding="utf-8")
    return Task(status=status, options={}, source_url="u", artifact_dir=str(tmp_path))


def test_can_resolve_speakers_true_when_matches_written(tmp_path):
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import TaskStatus
    task = _task_with_matches(tmp_path, status=TaskStatus.completed, write_matches=True)
    assert can_resolve_speakers_task(task) is True


def test_can_resolve_speakers_false_without_matches_file(tmp_path):
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import TaskStatus
    # A non-diarized task: match_speakers completed but wrote no speaker_matches.json,
    # so there is nothing to resolve and the dialog must stay hidden. This is the
    # bug — the button showed for such tasks because the old check keyed off the
    # step's completed status instead of the artifact the diarized path writes.
    task = _task_with_matches(tmp_path, status=TaskStatus.completed, write_matches=False)
    assert can_resolve_speakers_task(task) is False


def test_can_resolve_speakers_false_when_archived(tmp_path):
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import TaskStatus
    task = _task_with_matches(tmp_path, status=TaskStatus.archived, write_matches=True)
    assert can_resolve_speakers_task(task) is False


# ---------------------------------------------------------------------------
# is_noise persistence + transcript re-render on resolve (vts-552 task 10)
# ---------------------------------------------------------------------------


def _write_transcript_json(outputs, entries):
    (outputs / "transcript.json").write_text(
        json.dumps({"entries": entries, "text": ""}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_resolve_persists_is_noise_and_rerenders(client, authed_app, tmp_path):
    """Resolving SPEAKER_00 as noise must persist a MatchDecision with
    is_noise=True AND re-render transcript.txt to drop that speaker's lines,
    leaving the other speaker's text intact."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path)
    _override_diarization_backend(app, _FakeDiarizationBackend())

    outputs = tmp_path / "outputs"
    _write_transcript_json(
        outputs,
        [
            {"speaker": "SPEAKER_00", "text": "NOISE_TEXT_SHOULD_BE_DROPPED", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_01", "text": "KEEP_THIS_TEXT", "start": 1.0, "end": 2.0},
        ],
    )

    r = await client.post(
        f"/api/tasks/{task_id}/speakers",
        json={
            "resolutions": [
                {
                    "speaker_label": "SPEAKER_00",
                    "action": "leave_anonymous",
                    "add_fragment": False,
                    "outcome": "left_anonymous",
                    "is_noise": True,
                }
            ],
            "continue_task": False,
        },
    )
    assert r.status_code == 200, r.text

    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        labels = await repo.noise_labels_from_decisions(uuid.UUID(_TEST_USER_ID), task_id)
        assert "SPEAKER_00" in labels

    rendered = (outputs / "transcript.txt").read_text(encoding="utf-8")
    assert "NOISE_TEXT_SHOULD_BE_DROPPED" not in rendered
    assert "KEEP_THIS_TEXT" in rendered


@pytest.mark.asyncio
async def test_resolve_on_completed_does_not_requeue(client, authed_app, tmp_path):
    """A completed task past match_speakers can still be resolved (editing
    after the fact). continue_task=false must leave status=completed (no
    re-queue) while still re-rendering the transcript."""
    app, factory = authed_app
    task_id, _clip = await _seed_task_with_preview(factory, tmp_path, status="completed")
    _override_diarization_backend(app, _FakeDiarizationBackend())

    outputs = tmp_path / "outputs"
    _write_transcript_json(
        outputs,
        [
            {"speaker": "SPEAKER_00", "text": "SOME_LINE", "start": 0.0, "end": 1.0},
        ],
    )

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
        task = await repo.get_task_by_id(task_id)
        assert task.status == TaskStatus.completed

    # transcript re-rendered: transcript.txt exists and reflects current entries
    assert (outputs / "transcript.txt").read_text(encoding="utf-8").strip() == "SOME_LINE"


@pytest.mark.asyncio
async def test_speaker_matches_enriched_with_decisions_and_display_labels(
    client, authed_app, tmp_path
):
    # Bugs #1/#2 (vts-552 fast-follow): reopening the voice dialog must show the
    # operator's saved binding (not the stale auto-match) AND a display label
    # that matches the transcript ("Голос N" by first appearance), not the raw
    # SPEAKER_NN tag. get_speaker_matches must therefore merge in MatchDecision
    # state and a label_map-derived display_label.
    import json as _json
    from vts.db.models import StepStatus, TaskStatus
    from vts.db.repo import Repo

    app, factory = authed_app
    _ = app
    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    # speaker_matches.json: two speakers, keyed SPEAKER_00 / SPEAKER_01.
    matches = {
        "SPEAKER_00": {"outcome": "miss", "speaker_id": None, "distance": None,
                       "share": 0.6, "seconds": 60.0, "noise": False, "candidates": []},
        "SPEAKER_01": {"outcome": "miss", "speaker_id": None, "distance": None,
                       "share": 0.4, "seconds": 40.0, "noise": False, "candidates": []},
    }
    (outputs / "speaker_matches.json").write_text(_json.dumps(matches), encoding="utf-8")

    # transcript.json: SPEAKER_01 speaks FIRST, so its display label is "Голос 1"
    # and SPEAKER_00 is "Голос 2" — proving the label comes from appearance
    # order, NOT the technical tag order.
    transcript = {
        "entries": [
            {"speaker": "SPEAKER_01", "text": "первый", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_00", "text": "второй", "start": 1.0, "end": 2.0},
        ],
        "text": "",
    }
    (outputs / "transcript.json").write_text(_json.dumps(transcript), encoding="utf-8")

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/enrich",
            options={"diarize": True, "language": "ru"},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        step = await repo.upsert_step(task_id, "match_speakers")
        await repo.set_step_status(step, StepStatus.completed)
        await repo.set_task_status(task, TaskStatus.completed)
        # Operator bound SPEAKER_00 to a real person.
        person = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Виктор")
        await repo.record_decision(
            user_id=uuid.UUID(_TEST_USER_ID), source_task_id=task_id,
            speaker_label="SPEAKER_00", speaker_id=person.id, voice_sample_id=None,
            distance=None, embedding_model="ecapa", outcome="manual_match",
            is_noise=False,
        )
        await session.commit()
        person_id = str(person.id)

    r = await client.get(f"/api/tasks/{task_id}/speaker-matches")
    assert r.status_code == 200, r.text
    body = r.json()

    # Bug #1: the saved binding is reflected.
    assert body["SPEAKER_00"]["decided_speaker_id"] == person_id
    assert body["SPEAKER_00"]["decided_name"] == "Виктор"
    # SPEAKER_01 has no decision.
    assert body["SPEAKER_01"]["decided_speaker_id"] is None

    # Bug #2: display labels number by appearance in the transcript.
    assert body["SPEAKER_01"]["display_label"] == "Голос 1"
    assert body["SPEAKER_00"]["display_label"] == "Голос 2"


@pytest.mark.asyncio
async def test_speaker_matches_candidate_names_reflect_registry_rename(
    client, authed_app, tmp_path
):
    # A candidate name is frozen into speaker_matches.json at match time. If the
    # person is renamed in the registry afterwards, the reopened voice dialog
    # must show the CURRENT name, not the stale one — get_speaker_matches
    # reconciles candidate[].name against the live Speaker.name.
    import json as _json
    from vts.db.models import StepStatus, TaskStatus
    from vts.db.repo import Repo

    app, factory = authed_app
    _ = app
    task_id = uuid.uuid4()
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/rename",
            options={"diarize": True, "language": "ru"},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        step = await repo.upsert_step(task_id, "match_speakers")
        await repo.set_step_status(step, StepStatus.completed)
        person = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), "Старое Имя")
        person_id = str(person.id)
        await session.commit()

    # speaker_matches.json froze the OLD name into the candidate list.
    matches = {
        "SPEAKER_00": {
            "outcome": "auto", "speaker_id": person_id, "distance": 0.1,
            "share": 1.0, "seconds": 60.0, "noise": False,
            "candidates": [{"speaker_id": person_id, "name": "Старое Имя", "distance": 0.1}],
        },
    }
    (outputs / "speaker_matches.json").write_text(_json.dumps(matches), encoding="utf-8")

    # Operator renames the person in the registry after the match ran.
    async with factory() as session:
        repo = Repo(session)
        await repo.rename_speaker(uuid.UUID(_TEST_USER_ID), person.id, "Новое Имя")
        await session.commit()

    r = await client.get(f"/api/tasks/{task_id}/speaker-matches")
    assert r.status_code == 200, r.text
    body = r.json()
    cand = body["SPEAKER_00"]["candidates"][0]
    assert cand["name"] == "Новое Имя"
    # The id is unchanged — only the rendered name tracks the rename.
    assert cand["speaker_id"] == person_id
