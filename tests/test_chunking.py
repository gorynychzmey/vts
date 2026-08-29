"""Semantic chunking of a transcript (vts-twe7 / VOS-131).

The requirement is explicit: split on TIMESTAMPS and SPEAKER BOUNDARIES, not on
a fixed token count. The measured input says why neither extreme works — on a
real task (39350783, 91 entries): the median entry is 169 characters but 31 of
91 are under 100, and 5 run past 1000. So speaker turns alone are not chunks:
short ones must be merged and long ones split.

Splitting a long turn is safe — it belongs to ONE speaker, so no boundary is
crossed. Merging is the constrained direction: merging across a speaker change
would produce a chunk that misattributes who said what, which is exactly what
the search results would then cite.
"""
from __future__ import annotations

from vts.services.chunking import chunk_entries

MIN_CHARS = 100
MAX_CHARS = 1000


def _e(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def test_a_well_sized_turn_becomes_one_chunk():
    entries = [_e(0.0, 12.0, "x" * 300, "SPEAKER_00")]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "x" * 300
    assert chunks[0]["start"] == 0.0
    assert chunks[0]["end"] == 12.0
    assert chunks[0]["speakers"] == ["SPEAKER_00"]


def test_short_turns_of_the_same_speaker_are_merged():
    # 31 of 91 real entries are under 100 chars; alone they carry no retrievable
    # meaning.
    entries = [
        _e(0.0, 2.0, "Да.", "SPEAKER_00"),
        _e(2.0, 4.0, "Согласен.", "SPEAKER_00"),
        _e(4.0, 6.0, "Именно так и сделаем.", "SPEAKER_00"),
    ]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert "Да." in chunks[0]["text"] and "сделаем" in chunks[0]["text"]
    # The merged chunk spans the whole range it came from.
    assert chunks[0]["start"] == 0.0
    assert chunks[0]["end"] == 6.0


def test_merging_crosses_a_speaker_change_but_records_both():
    # A dialogue of one-liners must still become a usable chunk — but a chunk
    # that spans two voices has to say so, or a citation would attribute the
    # whole passage to whoever spoke first.
    entries = [
        _e(0.0, 2.0, "Ты закончил отчёт?", "SPEAKER_00"),
        _e(2.0, 4.0, "Почти, остался последний раздел.", "SPEAKER_01"),
    ]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert chunks[0]["speakers"] == ["SPEAKER_00", "SPEAKER_01"]


def test_a_long_turn_is_split():
    # 5 of 91 real entries exceed 1000 chars. Splitting is safe here: one
    # speaker, so no boundary is crossed.
    long_text = ". ".join(f"Предложение номер {i}" for i in range(200))
    entries = [_e(0.0, 300.0, long_text, "SPEAKER_00")]
    chunks = chunk_entries(entries)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= MAX_CHARS for c in chunks)
    # Every piece stays attributed to the one speaker it came from.
    assert all(c["speakers"] == ["SPEAKER_00"] for c in chunks)


def test_splitting_keeps_the_text_whole():
    long_text = ". ".join(f"Фраза {i}" for i in range(200))
    entries = [_e(0.0, 300.0, long_text, "SPEAKER_00")]
    chunks = chunk_entries(entries)
    rejoined = " ".join(c["text"] for c in chunks)
    # Nothing is dropped in the split; only whitespace between pieces may differ.
    assert rejoined.replace(" ", "") == long_text.replace(" ", "")


def test_split_timings_are_interpolated_within_the_turn():
    long_text = ". ".join(f"Фраза {i}" for i in range(200))
    entries = [_e(100.0, 400.0, long_text, "SPEAKER_00")]
    chunks = chunk_entries(entries)
    # Never outside the turn it came from, and monotonic.
    assert chunks[0]["start"] == 100.0
    assert chunks[-1]["end"] == 400.0
    for a, b in zip(chunks, chunks[1:]):
        assert a["end"] <= b["start"], "split chunks overlap in time"
        assert a["start"] < a["end"]


def test_an_undiarized_transcript_still_chunks():
    # Most transcripts have speaker=None — diarization is optional. Chunking is
    # not allowed to depend on it.
    entries = [
        _e(0.0, 10.0, "y" * 200),
        _e(10.0, 20.0, "z" * 200),
    ]
    chunks = chunk_entries(entries)
    assert chunks, "an undiarized transcript produced no chunks"
    assert all(c["speakers"] == [] for c in chunks)


def test_empty_and_blank_entries_are_ignored():
    entries = [
        _e(0.0, 1.0, "   ", "SPEAKER_00"),
        _e(1.0, 2.0, "", "SPEAKER_00"),
        _e(2.0, 14.0, "w" * 200, "SPEAKER_00"),
    ]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "w" * 200
    # The chunk's time range comes from what it actually contains, not from the
    # silence that preceded it.
    assert chunks[0]["start"] == 2.0


def test_no_entries_yields_no_chunks():
    assert chunk_entries([]) == []
    assert chunk_entries(None) == []


def test_entries_without_timings_are_skipped_not_guessed():
    entries = [
        {"text": "no timings here", "speaker": "SPEAKER_00"},
        _e(5.0, 17.0, "q" * 200, "SPEAKER_00"),
    ]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert chunks[0]["start"] == 5.0


def test_chunks_carry_their_index_in_order():
    entries = [_e(i * 10.0, i * 10.0 + 10.0, chr(97 + i) * 200, "SPEAKER_00") for i in range(3)]
    chunks = chunk_entries(entries)
    assert [c["index"] for c in chunks] == list(range(len(chunks)))


def test_a_real_sized_transcript_produces_sane_chunks():
    # Shaped like the measured task: median 169 chars, a third under 100, a few
    # over 1000, two speakers alternating.
    entries = []
    t = 0.0
    for i in range(91):
        if i % 3 == 0:
            text = "Коротко."                       # under 100
        elif i % 17 == 0:
            text = ". ".join(f"Длинная мысль {j}" for j in range(90))  # over 1000
        else:
            text = "Средняя реплика про результаты и планы. " * 4       # ~160
        entries.append(_e(t, t + 12.0, text, f"SPEAKER_0{i % 2}"))
        t += 12.0

    chunks = chunk_entries(entries)
    assert chunks
    # No chunk is uselessly small or over the cap.
    assert all(len(c["text"]) <= MAX_CHARS for c in chunks), "a chunk exceeded the cap"
    body = [c for c in chunks[:-1]]  # the tail may legitimately be short
    assert all(len(c["text"]) >= MIN_CHARS for c in body), "a sub-minimum chunk survived"
    # Time never runs backwards across the whole transcript.
    for a, b in zip(chunks, chunks[1:]):
        assert a["start"] <= b["start"]


# --------------------------------------- shapes only real transcripts reveal

def test_newlines_inside_an_entry_are_normalised_away():
    """Measured on the real corpus: 99% of chunks carried raw newlines.

    Whisper wraps its output, so an entry's text is full of line breaks that
    mean nothing about the speech. They survive into the chunk, and a citation
    then renders as a ragged column — the chunk is what a search result quotes,
    so its text has to read as prose.
    """
    entries = [_e(0.0, 12.0, "докладывали о том\nчто макс и\nостальные пришли " + "x" * 150,
                  "SPEAKER_00")]
    chunks = chunk_entries(entries)
    assert "\n" not in chunks[0]["text"]
    assert "докладывали о том что макс и остальные пришли" in chunks[0]["text"]


def test_a_tiny_trailing_chunk_is_absorbed_rather_than_emitted():
    """The real corpus produced a 17-character chunk.

    A fragment that short retrieves nothing useful and dilutes the corpus; it
    belongs on the end of its predecessor. Only when there is no predecessor —
    a transcript that IS one short line — does it stand alone, because dropping
    it would lose the recording's only content.
    """
    entries = [
        _e(0.0, 12.0, "y" * 600, "SPEAKER_00"),
        _e(12.0, 14.0, "Ага, понятно.", "SPEAKER_00"),
    ]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1, f"a tiny tail survived as its own chunk: {[len(c['text']) for c in chunks]}"
    assert chunks[0]["text"].endswith("Ага, понятно.")
    assert chunks[0]["end"] == 14.0


def test_a_transcript_that_is_only_one_short_line_still_yields_it():
    entries = [_e(0.0, 2.0, "Всем спасибо.", "SPEAKER_00")]
    chunks = chunk_entries(entries)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Всем спасибо."


def test_splitting_does_not_leave_a_tiny_remainder_in_the_middle():
    """The real corpus produced 16-character chunks in the MIDDLE of recordings.

    They came from splitting a long turn: the last piece can fall well under
    the floor, and unlike a trailing chunk it is surrounded by content, so the
    absorb-the-tail rule never saw it. A fragment like "Очень небольшой."
    retrieves nothing and dilutes the corpus.

    The fix belongs in the splitter: a remainder too small to stand goes back
    onto the previous piece, which is why the cap has to leave room for it.
    """
    # Shaped like the real entry that produced it (task 39350783): 1033
    # characters of speech whose final clause has no sentence end, so the
    # splitter's last piece is a stub.
    body = ". ".join(f"Довольно длинное предложение под номером {i}" for i in range(23))
    entries = [_e(0.0, 120.0, body + ". Ну, вот именно так я и такую обратную связь и",
                  "SPEAKER_00")]
    chunks = chunk_entries(entries)
    assert len(chunks) > 1, "the fixture no longer splits; it proves nothing"
    assert all(len(c["text"]) >= MIN_CHARS for c in chunks), (
        f"a sub-minimum piece survived the split: {[len(c['text']) for c in chunks]}"
    )
    assert all(len(c["text"]) <= MAX_CHARS for c in chunks)


def test_a_short_entry_between_others_is_merged_not_stranded():
    """Also from the real corpus: a 77-character entry became its own chunk.

    It is not a tail, so absorbing tails never reached it, and it cleared no
    threshold of its own — it simply sat between two chunks that had each just
    closed. A chunk that short answers nothing.
    """
    entries = [
        _e(0.0, 20.0, "a" * 520, "SPEAKER_00"),
        _e(20.0, 24.0, "кипя и там же ключевое о чем говорится что у вас вырастет товарообороты",
           "SPEAKER_00"),
        _e(24.0, 44.0, "b" * 520, "SPEAKER_00"),
    ]
    chunks = chunk_entries(entries)
    assert all(len(c["text"]) >= MIN_CHARS for c in chunks), (
        f"a short middle entry was stranded: {[len(c['text']) for c in chunks]}"
    )


def test_a_short_entry_goes_forward_when_the_previous_chunk_is_full():
    """Measured on the whole real corpus, then fixed here.

    For 20 of the 21 surviving sub-minimum chunks the PREVIOUS chunk was
    already 984-998 characters — there was no room in it. Prepending to the
    next chunk is the remaining home. What is left after this (15 of 6630,
    0.2%) has both neighbours full, and buying those back would mean loosening
    the cap, which costs more than the fragments do.
    """
    entries = [
        _e(0.0, 20.0, "a" * 980, "SPEAKER_00"),          # predecessor: nearly at the cap
        _e(20.0, 22.0, "короткая вставка", "SPEAKER_01"),  # 16 chars, nowhere behind
        _e(22.0, 42.0, "b" * 500, "SPEAKER_00"),          # successor: has room
    ]
    chunks = chunk_entries(entries)
    assert all(len(c["text"]) >= MIN_CHARS for c in chunks), (
        f"a short entry stayed stranded: {[len(c['text']) for c in chunks]}"
    )
    assert all(len(c["text"]) <= MAX_CHARS for c in chunks)
    # It joined the FOLLOWING chunk, so that chunk now starts where the short
    # entry did — a citation must point at the audio the text came from.
    joined = [c for c in chunks if "короткая вставка" in c["text"]]
    assert joined and joined[0]["start"] == 20.0
    # And the chunk now spans two voices, which it has to declare.
    assert joined[0]["speakers"] == ["SPEAKER_01", "SPEAKER_00"]
