import json
import logging
import uuid
from pathlib import Path

import pytest

from vts.pipeline.rerender import resolve_noise_labels, rerender_transcript


def test_resolve_noise_prefers_decisions_when_present():
    matches = {"A": {"noise": True}, "B": {"noise": False}}
    # decisions exist -> use them, ignore the auto suggestion in matches
    assert resolve_noise_labels(matches, {"B"}, has_decisions=True) == {"B"}


def test_resolve_noise_falls_back_to_matches_without_decisions():
    matches = {"A": {"noise": True}, "B": {"noise": False}}
    assert resolve_noise_labels(matches, set(), has_decisions=False) == {"A"}


def test_resolve_noise_empty_matches():
    assert resolve_noise_labels({}, set(), has_decisions=False) == set()


def _transcript_payload() -> dict:
    return {
        "entries": [
            {"speaker": "SPEAKER_00", "text": "привет как дела", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "text": "шум шум шум", "start": 2.0, "end": 4.0},
            {"speaker": "SPEAKER_00", "text": "все хорошо", "start": 4.0, "end": 6.0},
        ],
        "text": "placeholder text before rerender",
    }


def _seed_outputs(tmp_path: Path, *, all_noise: bool = False) -> Path:
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "transcript.json").write_text(
        json.dumps(_transcript_payload()), encoding="utf-8"
    )
    noisy_labels = ["SPEAKER_00", "SPEAKER_01"] if all_noise else ["SPEAKER_01"]
    matches = {
        label: {"noise": True, "share": 0.5} for label in noisy_labels
    }
    if not all_noise:
        matches["SPEAKER_00"] = {"noise": False, "share": 0.5}
    (outputs / "speaker_matches.json").write_text(json.dumps(matches), encoding="utf-8")
    return outputs


@pytest.mark.asyncio
async def test_rerender_transcript_excludes_noise_and_is_idempotent(
    authed_app, tmp_path, caplog
):
    from tests.conftest import _TEST_USER_ID
    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    app, factory = authed_app
    _ = app

    outputs = _seed_outputs(tmp_path)

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={},
            artifact_dir=str(tmp_path),
        )
        await repo.set_task_status(task, TaskStatus.awaiting_input)
        # Decision marking SPEAKER_01 as noise -- decisions exist, so
        # resolve_noise_labels must prefer them over speaker_matches.json.
        await repo.record_decision(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_task_id=task.id,
            speaker_label="SPEAKER_01",
            speaker_id=None,
            voice_sample_id=None,
            distance=None,
            embedding_model="ecapa",
            outcome="noise",
            is_noise=True,
        )
        await session.commit()
        task_id = task.id

    async with factory() as session:
        task_row = await Repo(session).get_task_by_id(task_id)
        await rerender_transcript(task_row, session, language="ru")

    payload = json.loads((outputs / "transcript.json").read_text(encoding="utf-8"))
    entries = payload["entries"]
    speakers = {e["speaker"] for e in entries}
    assert "SPEAKER_01" not in speakers
    assert speakers == {"SPEAKER_00"}

    txt = (outputs / "transcript.txt").read_text(encoding="utf-8")
    assert "SPEAKER_01" not in txt
    assert "шум" not in txt
    assert "привет как дела" in txt
    assert "все хорошо" in txt
    assert payload["text"] == txt

    json_bytes_first = (outputs / "transcript.json").read_bytes()
    txt_bytes_first = (outputs / "transcript.txt").read_bytes()

    # Idempotency: calling again must be byte-identical.
    async with factory() as session:
        task_row = await Repo(session).get_task_by_id(task_id)
        await rerender_transcript(task_row, session, language="ru")

    json_bytes_second = (outputs / "transcript.json").read_bytes()
    txt_bytes_second = (outputs / "transcript.txt").read_bytes()
    assert json_bytes_first == json_bytes_second
    assert txt_bytes_first == txt_bytes_second


@pytest.mark.asyncio
async def test_rerender_transcript_empty_guard_renders_all_and_warns(
    authed_app, tmp_path, caplog
):
    from tests.conftest import _TEST_USER_ID
    from vts.db.models import TaskStatus
    from vts.db.repo import Repo

    app, factory = authed_app
    _ = app

    outputs = _seed_outputs(tmp_path, all_noise=True)

    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v2",
            options={},
            artifact_dir=str(tmp_path),
        )
        await repo.set_task_status(task, TaskStatus.awaiting_input)
        for label in ("SPEAKER_00", "SPEAKER_01"):
            await repo.record_decision(
                user_id=uuid.UUID(_TEST_USER_ID),
                source_task_id=task.id,
                speaker_label=label,
                speaker_id=None,
                voice_sample_id=None,
                distance=None,
                embedding_model="ecapa",
                outcome="noise",
                is_noise=True,
            )
        await session.commit()
        task_id = task.id

    async with factory() as session:
        task_row = await Repo(session).get_task_by_id(task_id)
        with caplog.at_level(logging.WARNING, logger="vts.pipeline.rerender"):
            await rerender_transcript(task_row, session, language="ru")

    assert any(
        "rerender_transcript" in rec.message and "noise" in rec.message
        for rec in caplog.records
    )

    payload = json.loads((outputs / "transcript.json").read_text(encoding="utf-8"))
    entries = payload["entries"]
    assert len(entries) == 3
    speakers = {e["speaker"] for e in entries}
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}

    txt = (outputs / "transcript.txt").read_text(encoding="utf-8")
    assert txt.strip() != ""
