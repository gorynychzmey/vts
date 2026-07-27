"""Two-level player transcript (vts-u6w): the /player page keeps the existing
BLOCK structure (an ASR segment for undiarized audio, a speaker turn for
diarized) but makes the SENTENCES inside each block individually clickable, so
a click seeks to that sentence rather than to the start of a 5-minute chunk.

`build_player_blocks` is the pure builder:
  * blocks come from transcript.json entries (already speaker-attributed);
  * sentences come from the inner ASR segments in segments_raw.json, whose
    timings are CHUNK-LOCAL and must be shifted by the chunk offset, then
    matched into the block whose time window they fall in;
  * each block's display label is the registry name / "Голос N", never the raw
    SPEAKER_NN tag (fixes the diarized-name bug);
  * a block with no matching inner timings stays one clickable whole (graceful
    fallback for backends without word/segment timings).
"""
from __future__ import annotations

from vts.services.player_transcript import build_player_blocks


def _raw(segment_index, offset, inner):
    return {"segment_index": segment_index, "start": offset, "end": offset + 1000,
            "raw_json": {"segments": inner}}


def test_block_split_into_clickable_sentences_with_absolute_times():
    # One undiarized block spanning [0, 10]; its chunk's inner ASR segments are
    # chunk-local and must surface as absolute-time sentences.
    entries = [{"start": 0.0, "end": 10.0, "text": "Hello there. How are you?"}]
    raw = [_raw(1, 0.0, [
        {"start": 0.0, "end": 2.0, "text": "Hello there."},
        {"start": 2.0, "end": 5.0, "text": "How are you?"},
    ])]
    blocks = build_player_blocks(entries, raw, names={}, label_word="Speaker")
    assert len(blocks) == 1
    b = blocks[0]
    assert [s["text"] for s in b["sentences"]] == ["Hello there.", "How are you?"]
    assert b["sentences"][0]["start"] == 0.0
    assert b["sentences"][1]["start"] == 2.0


def test_inner_times_shifted_by_chunk_offset():
    # A later chunk: offset 300, inner times are 0-based within the chunk.
    entries = [{"start": 300.0, "end": 310.0, "text": "Later block"}]
    raw = [_raw(2, 300.0, [
        {"start": 0.0, "end": 3.0, "text": "Later"},
        {"start": 3.0, "end": 6.0, "text": "block"},
    ])]
    blocks = build_player_blocks(entries, raw, names={}, label_word="Speaker")
    starts = [s["start"] for s in blocks[0]["sentences"]]
    assert starts == [300.0, 303.0]  # shifted into absolute time


def test_diarized_block_label_uses_registry_name_not_speaker_tag():
    # Two speaker-turn blocks. The player must show the resolved label, never
    # the raw SPEAKER_NN tag.
    entries = [
        {"start": 0.0, "end": 5.0, "text": "Hi", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 9.0, "text": "Hello", "speaker": "SPEAKER_01"},
    ]
    raw = [_raw(1, 0.0, [
        {"start": 0.0, "end": 5.0, "text": "Hi"},
        {"start": 5.0, "end": 9.0, "text": "Hello"},
    ])]
    blocks = build_player_blocks(
        entries, raw, names={"SPEAKER_00": "Alice"}, label_word="Голос"
    )
    # SPEAKER_00 -> registry name; SPEAKER_01 -> numbered label, never raw tag.
    assert blocks[0]["label"] == "Alice"
    assert blocks[1]["label"] == "Голос 2"
    assert "SPEAKER_00" not in blocks[0]["label"]
    assert "SPEAKER_01" not in blocks[1]["label"]


def test_block_without_inner_timings_is_one_clickable_whole():
    # No raw segments at all -> each block is a single clickable sentence at the
    # block's own start (graceful fallback, no fabricated sub-timings).
    entries = [{"start": 0.0, "end": 300.0, "text": "A long chunk of text"}]
    blocks = build_player_blocks(entries, [], names={}, label_word="Speaker")
    assert len(blocks) == 1
    b = blocks[0]
    assert len(b["sentences"]) == 1
    assert b["sentences"][0]["start"] == 0.0
    assert b["sentences"][0]["text"] == "A long chunk of text"


def test_undiarized_block_has_no_label():
    entries = [{"start": 0.0, "end": 5.0, "text": "solo"}]
    raw = [_raw(1, 0.0, [{"start": 0.0, "end": 5.0, "text": "solo"}])]
    blocks = build_player_blocks(entries, raw, names={}, label_word="Speaker")
    # No speaker -> no label prefix (monologue).
    assert not blocks[0].get("label")
