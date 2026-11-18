"""A task whose voices all match finishes with their names in the transcript.

Names are rendered from saved decisions, and matching saved none: the task
completed as "Speaker 1 / Speaker 2" although both voices were recognised, and
the names appeared only after the user opened the voice dialog and pressed
Save (2026-11-18). Checked here on the rendered transcript, which is what the
user reads, against the real database.
"""
from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import pytest

from tests.conftest import _TEST_USER_ID
from vts.pipeline.steps.speaker_match import MatchSpeakersStep

ALICE_VOICE = [1.0] + [0.0] * 255
BOB_VOICE = [0.0, 1.0] + [0.0] * 254


@pytest.mark.asyncio
async def test_matched_voices_are_named_in_the_transcript(authed_app, tmp_path):
    _app, factory = authed_app
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "diarization.json").write_text(json.dumps({
        "embedding_model": "ecapa",
        "embeddings": {"SPEAKER_00": ALICE_VOICE, "SPEAKER_01": BOB_VOICE},
        "segments": [
            {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
            {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_01"},
        ],
    }), encoding="utf-8")
    entries = [
        {"start": 0.0, "end": 10.0, "text": "guten morgen", "speaker": "SPEAKER_00"},
        {"start": 10.0, "end": 20.0, "text": "hallo zusammen", "speaker": "SPEAKER_01"},
    ]
    (outputs / "transcript.json").write_text(json.dumps({
        "text": "Speaker 1: guten morgen\n\nSpeaker 2: hallo zusammen",
        "entries": entries, "all_entries": entries,
    }), encoding="utf-8")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID), source_url="file://x.mkv",
            options={"diarize": True, "language": "de"}, artifact_dir=str(tmp_path), task_id=task_id,
        )
        for name, voice in (("Alice", ALICE_VOICE), ("Bob", BOB_VOICE)):
            sp = await repo.create_speaker(uuid.UUID(_TEST_USER_ID), name)
            await repo.add_voice_sample(
                speaker_id=sp.id, embedding=voice, embedding_model="ecapa",
                audio=b"RIFF", audio_format="wav", duration_sec=5.0, source_task_id=None,
            )
        await session.commit()

    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            diarization_enabled_default=False,
            speaker_match_max_distance_auto=0.25,
            speaker_match_max_distance_candidate=0.55,
            speaker_match_candidates_cap=100,
            diarization_min_speaker_share=0.05,
            diarization_noise_max_distance=0.25,
            embedding_enabled=False,
        ),
        session_factory=factory,
        task_flag=lambda options, key, *, default: bool(options.get(key, default)),
    )
    st = SimpleNamespace(
        task_id=task_id, user_id=_TEST_USER_ID, task_options={"diarize": True, "language": "de"},
        dirs={"outputs": outputs}, logger=logging.getLogger("test"),
    )

    assert await MatchSpeakersStep().run(ctx, st) is True
    assert (outputs / "transcript.txt").read_text(encoding="utf-8") == (
        "Alice: guten morgen\n\nBob: hallo zusammen"
    )
