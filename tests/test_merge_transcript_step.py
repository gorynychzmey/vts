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

    async def speaker_names_for_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> dict[str, str]:
        # No registry matches in this fixture — the step must render "Голос N".
        return {}


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
        # The real pipeline always passes str(task.user_id) — a UUID string —
        # and the step parses it back to look up registry names.
        task_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test_merge_transcript_step"),
        # Explicit language so the rendered label matches the fixture's actual
        # language (Russian) -- in the real pipeline this always comes from
        # DetectLanguageStep having already run before MergeTranscriptStep.
        task_options={"language": "ru"},
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


def test_merge_transcript_step_renders_registry_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the step: a voice matched to a registry person renders
    as that person's name in transcript.json, while an unmatched voice keeps its
    numbered label. Covers the step's DB lookup, which the pure-function tests
    of apply_diarization cannot reach."""
    dirs = _dirs(tmp_path)
    segments = _segments_with_one_silent_chunk()
    ctx = _ctx(monkeypatch, segments)

    # Re-patch Repo so speaker_names_for_task reports a match for SPEAKER_00.
    class _NamingRepo(_FakeRepo):
        async def speaker_names_for_task(
            self, user_id: uuid.UUID, task_id: uuid.UUID
        ) -> dict[str, str]:
            return {"SPEAKER_00": "Вася"}

    monkeypatch.setattr(
        "vts.pipeline.steps.transcription.Repo",
        lambda session: _NamingRepo(session, segments),
    )

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
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test_merge_transcript_step"),
        task_options={"language": "ru"},
    )

    assert asyncio.run(MergeTranscriptStep().run(ctx, st)) is True

    payload = json.loads((dirs["outputs"] / "transcript.json").read_text(encoding="utf-8"))
    # Technical tags stay in the entries — substitution is render-time only.
    assert [e["speaker"] for e in payload["entries"]] == [
        "SPEAKER_00", "SPEAKER_00", "SPEAKER_01",
    ]
    assert payload["text"] == "Вася: привет мир как быстро\n\nГолос 2: у дела"


def _segments_for_marginal_speaker_regression() -> list[_FakeSegment]:
    """Three ASR entries, none carrying word-level timestamps (empty raw_json),
    so every entry is attributed to a speaker by whole-entry maximum overlap
    (`speaker_at`) rather than a word-level split — the same fallback path
    `drop_marginal_speakers` measures "share" over via each entry's own
    `end - start` span.

    entries[0] spans the SPEAKER_00 diarization block (0.0-87.0s, 87s).
    entries[1] and entries[2] are two short 1.5s entries that each fully
    overlap one of SPEAKER_03's diarization turns, so both get attributed to
    SPEAKER_03. Their combined ASR-entry span is 3.0s out of 90.0s total entry
    span (~3.3%) — well under the default 5% `diarization_min_speaker_share`.

    Reproduces vts-0ws: SPEAKER_03 genuinely holds 13% of DIARIZED time (see
    `_diarization_for_marginal_speaker_regression`), but the ASR chunker only
    produced short, recognizable-word entries for two of SPEAKER_03's turns —
    it produced no entry at all for the 88.5-89.0s gap or the long
    90.5-100.0s tail where SPEAKER_03 kept talking without ASR text landing on
    it. Measuring "share" over ASR-entry span (3.3%) instead of diarized time
    (13%) is exactly the discrepancy `drop_marginal_speakers` used to
    mismeasure, folding a real speaker into the dominant one.
    """
    return [
        _FakeSegment(segment_index=1, start_sec=0.0, end_sec=87.0, text="привет как дела у тебя", raw_json={}),
        _FakeSegment(segment_index=2, start_sec=87.0, end_sec=88.5, text="ага", raw_json={}),
        _FakeSegment(segment_index=3, start_sec=89.0, end_sec=90.5, text="угу", raw_json={}),
    ]


def _diarization_for_marginal_speaker_regression() -> dict:
    """SPEAKER_00 holds 87.0s (87%) of diarized time; SPEAKER_03 holds 13.0s
    (13%), spread across four turns — two of which (87.0-88.5 and 89.0-90.5)
    line up with the short ASR entries above, and two of which (88.5-89.0,
    90.5-100.0) fall in the gaps where the ASR chunker produced no entry at
    all. `drop_marginal_speakers` never sees diarization segments directly —
    only the entries — so these gap turns are exactly what the ASR-entry-span
    measurement misses.
    """
    return {
        "segments": [
            {"start": 0.0, "end": 87.0, "speaker": "SPEAKER_00"},
            {"start": 87.0, "end": 88.5, "speaker": "SPEAKER_03"},
            {"start": 88.5, "end": 89.0, "speaker": "SPEAKER_03"},
            {"start": 89.0, "end": 90.5, "speaker": "SPEAKER_03"},
            {"start": 90.5, "end": 100.0, "speaker": "SPEAKER_03"},
        ]
    }


def test_merge_transcript_step_does_not_fold_a_marginal_by_asr_span_speaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for vts-0ws: SPEAKER_03 holds 13% of DIARIZED time but its
    turns land in short ASR entries summing to only ~3.3% of ASR-entry span.
    `drop_marginal_speakers` measured share over entry span, so it used to
    fold SPEAKER_03 into the dominant SPEAKER_00 even though SPEAKER_03 is a
    real, substantial speaker. The live merge path now calls
    `apply_diarization` with `min_share=0.0`, making the fold a no-op — both
    speakers must survive in the rendered transcript.json entries."""
    dirs = _dirs(tmp_path)
    segments = _segments_for_marginal_speaker_regression()
    ctx = _ctx(monkeypatch, segments)

    diar_path = dirs["outputs"] / "diarization.json"
    diar_path.write_text(
        json.dumps(_diarization_for_marginal_speaker_regression()),
        encoding="utf-8",
    )

    st = StepState(
        task_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test_merge_transcript_step"),
        task_options={"language": "ru"},
    )

    assert asyncio.run(MergeTranscriptStep().run(ctx, st)) is True

    payload = json.loads((dirs["outputs"] / "transcript.json").read_text(encoding="utf-8"))
    speakers = {e.get("speaker") for e in payload["entries"]}
    assert "SPEAKER_00" in speakers
    assert "SPEAKER_03" in speakers  # the 13%-real speaker must NOT be folded
