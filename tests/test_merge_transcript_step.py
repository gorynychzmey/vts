"""Exercises MergeTranscriptStep.run over the real loop that builds
raw_json_by_index, rather than hand-building the dict and calling
apply_diarization directly (which never observes how the loop populates it).

Regression target: keying raw_json_by_index by enumerate(segments) instead of
len(entries) desynchronises the map whenever a segment has empty text (a
silent chunk), scattering speakers onto the wrong entries. See vts-5xz Finding 2.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.base import StepState
from vts.pipeline.steps.transcription import MergeTranscriptStep


class _FakeSegment:
    def __init__(self, segment_index: int, start_sec: float, end_sec: float, text: str, raw_json: dict) -> None:
        self.segment_index = segment_index
        self.start_sec = start_sec
        self.end_sec = end_sec
        self.text = text
        self.raw_json = raw_json


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def commit(self) -> None:
        return None


class _FakeRepo:
    """Stand-in for vts.db.repo.Repo, monkeypatched into the step module.

    Only implements what MergeTranscriptStep.run actually calls.
    """

    def __init__(self, session: object, segments: list[_FakeSegment]) -> None:
        self.session = session
        self._segments = segments

    async def get_task_segments(self, task_id: uuid.UUID) -> list[_FakeSegment]:
        return self._segments

    async def get_task_by_id(self, task_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(transcript_path=None)


class _DummyBus:
    async def publish_event(self, **kwargs: object) -> None:
        return None


def _dirs(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "task"
    for name in ("outputs", "segments"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return {"root": root, "outputs": root / "outputs", "segments": root / "segments"}


def _segments_with_one_silent_chunk() -> list[_FakeSegment]:
    """Three ASR chunks; chunk index 1 (0-based) is silent (empty text) but
    still carries a (non-empty) raw_json — e.g. Whisper returned a words list
    for a chunk whose text later got stripped to "" (silence/noise token).

    Real entries end up as: entries[0] <- chunk 0, entries[1] <- chunk 2
    (chunk 1 produced no entry, so it must NOT occupy a slot in
    raw_json_by_index at all). Correct keying maps raw_json_by_index using the
    ENTRY index (len(entries) at append time): {0: chunk0 raw, 1: chunk2 raw}.

    The buggy `enumerate(segments)` keying instead produces
    {0: chunk0 raw, 1: chunk1 raw, 2: chunk2 raw} — chunk1's raw_json (words
    anchored at 2.0-6.0, entirely outside entries[1]'s real time range of
    6.0-9.0) lands at key 1. merge_entries then reads raw_json_by_index.get(1)
    for entries[1] (chunk2's actual text "как дела"), gets chunk1's words
    instead of chunk2's, filters them to entry_words by overlap with
    entries[1]'s [6.0, 9.0] window — chunk1's words (2.0-6.0) don't overlap
    that window at all — so entry_words comes back empty and the whole entry
    falls back to maximum-overlap-by-time across its full [6.0, 9.0] span. The
    diarization layout below gives SPEAKER_00 the larger overlap of that whole
    span (2.5s vs 0.5s), while the correct word-level split (using chunk2's
    OWN words) puts "как" with SPEAKER_00 and "дела" with SPEAKER_01 — two
    entries, not one — so the two code paths produce observably different
    entry counts and speakers.
    """
    chunk0_raw = {
        "segments": [
            {
                "words": [
                    {"word": "привет", "start": 0.0, "end": 1.0},
                    {"word": "мир", "start": 1.0, "end": 2.0},
                ]
            }
        ]
    }
    # Silent chunk's own (unused-by-correct-code) words, well outside entries[1]'s
    # time range — if wrongly attributed to entries[1], they contribute nothing
    # inside its window, forcing the whole-entry fallback path.
    chunk1_raw = {
        "segments": [
            {
                "words": [
                    {"word": "шум", "start": 2.0, "end": 6.0},
                ]
            }
        ]
    }
    chunk2_raw = {
        "segments": [
            {
                "words": [
                    {"word": "как", "start": 6.0, "end": 6.5},
                    {"word": "быстро", "start": 6.5, "end": 7.0},
                    {"word": "у", "start": 8.0, "end": 8.5},
                    {"word": "дела", "start": 8.5, "end": 9.0},
                ]
            }
        ]
    }
    return [
        _FakeSegment(segment_index=1, start_sec=0.0, end_sec=2.0, text="привет мир", raw_json=chunk0_raw),
        _FakeSegment(segment_index=2, start_sec=2.0, end_sec=6.0, text="", raw_json=chunk1_raw),
        _FakeSegment(segment_index=3, start_sec=6.0, end_sec=9.0, text="как быстро у дела", raw_json=chunk2_raw),
    ]


def _ctx(monkeypatch: pytest.MonkeyPatch, segments: list[_FakeSegment]) -> SimpleNamespace:
    def _repo_factory(session: object) -> _FakeRepo:
        return _FakeRepo(session, segments)

    monkeypatch.setattr("vts.pipeline.steps.transcription.Repo", _repo_factory)

    return SimpleNamespace(
        session_factory=lambda: _FakeSession(),
        bus=_DummyBus(),
        settings=SimpleNamespace(
            diarization_min_words=2,
            diarization_min_seconds=0.8,
            diarization_min_speaker_share=0.05,
        ),
    )


def test_merge_transcript_step_attributes_speakers_correctly_across_a_silent_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = _dirs(tmp_path)
    segments = _segments_with_one_silent_chunk()
    ctx = _ctx(monkeypatch, segments)

    diar_path = dirs["outputs"] / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
                    {"start": 6.0, "end": 8.0, "speaker": "SPEAKER_00"},
                    {"start": 8.0, "end": 9.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )

    st = StepState(
        task_id=uuid.uuid4(),
        user_id="user-1",
        dirs=dirs,
        logger=logging.getLogger("test_merge_transcript_step"),
        task_options={},
    )

    success = asyncio.run(MergeTranscriptStep().run(ctx, st))
    assert success is True

    payload = json.loads((dirs["outputs"] / "transcript.json").read_text(encoding="utf-8"))
    entries = payload["entries"]

    # Correct keying: chunk2's OWN words (not chunk1's) drive the split, so
    # "как быстро" (SPEAKER_00) and "у дела" (SPEAKER_01) become two entries.
    # Under the reverted enumerate(segments) keying, entries[1] gets chunk1's
    # words instead (which fall entirely outside its [6.0, 9.0] time range),
    # so entry_words comes back empty, forcing a whole-entry maximum-overlap
    # fallback that collapses to ONE entry attributed to SPEAKER_00 — losing
    # "у дела" 's real SPEAKER_01 attribution entirely. This assertion fails
    # under that reverted keying.
    assert [e["text"] for e in entries] == ["привет мир", "как быстро", "у дела"]
    assert entries[0]["speaker"] == "SPEAKER_00"
    assert entries[1]["speaker"] == "SPEAKER_00"
    assert entries[2]["speaker"] == "SPEAKER_01"
    assert payload["text"] == "Голос 1: привет мир как быстро\n\nГолос 2: у дела"
