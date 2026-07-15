from vts.services.diarization.merge import drop_marginal_speakers, label_map, render_transcript


def test_label_map_orders_by_first_appearance() -> None:
    entries = [
        {"start": 0.0, "end": 1.0, "text": "a", "speaker": "SPEAKER_01"},
        {"start": 1.0, "end": 2.0, "text": "b", "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 3.0, "text": "c", "speaker": "SPEAKER_01"},
    ]
    # SPEAKER_01 speaks first, so it is "Голос 1" regardless of its numeric tag.
    assert label_map(entries) == {"SPEAKER_01": "Голос 1", "SPEAKER_00": "Голос 2"}


def test_drop_marginal_speakers_removes_noise_speaker() -> None:
    # SPEAKER_09 holds 0.5s of 100.5s (~0.5%) — a phantom from music or echo.
    entries = [
        {"start": 0.0, "end": 100.0, "text": "долгая речь", "speaker": "SPEAKER_00"},
        {"start": 100.0, "end": 100.5, "text": "шум", "speaker": "SPEAKER_09"},
    ]
    result = drop_marginal_speakers(entries, min_share=0.05)
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_00"]


def test_drop_marginal_speakers_keeps_real_speakers() -> None:
    entries = [
        {"start": 0.0, "end": 60.0, "text": "первый", "speaker": "SPEAKER_00"},
        {"start": 60.0, "end": 100.0, "text": "второй", "speaker": "SPEAKER_01"},
    ]
    result = drop_marginal_speakers(entries, min_share=0.05)
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]


def test_render_single_speaker_is_flat_text() -> None:
    entries = [
        {"start": 0.0, "end": 5.0, "text": "первая фраза", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 9.0, "text": "вторая фраза", "speaker": "SPEAKER_00"},
    ]
    assert render_transcript(entries, min_share=0.05) == "первая фраза вторая фраза"


def test_render_dialogue_labels_on_speaker_change() -> None:
    entries = [
        {"start": 0.0, "end": 5.0, "text": "привет", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 9.0, "text": "как дела", "speaker": "SPEAKER_00"},
        {"start": 9.0, "end": 14.0, "text": "нормально", "speaker": "SPEAKER_01"},
    ]
    assert render_transcript(entries, min_share=0.05) == (
        "Голос 1: привет как дела\n\nГолос 2: нормально"
    )


def test_render_phantom_speaker_collapses_to_flat_text() -> None:
    # The phantom is dropped, one speaker remains -> monologue, no labels at all.
    entries = [
        {"start": 0.0, "end": 100.0, "text": "долгая речь", "speaker": "SPEAKER_00"},
        {"start": 100.0, "end": 100.5, "text": "шум", "speaker": "SPEAKER_09"},
    ]
    assert render_transcript(entries, min_share=0.05) == "долгая речь шум"


def test_render_without_speakers_is_flat_text() -> None:
    entries = [
        {"start": 0.0, "end": 5.0, "text": "первая", "speaker": None},
        {"start": 5.0, "end": 9.0, "text": "вторая", "speaker": None},
    ]
    assert render_transcript(entries, min_share=0.05) == "первая вторая"


def test_drop_marginal_speakers_keeps_speaker_at_exact_min_share() -> None:
    # 5.0s of 100.0s is EXACTLY min_share=0.05. The cutoff is `<` (exclusive),
    # so a speaker sitting exactly on the boundary must survive, not be dropped.
    entries = [
        {"start": 0.0, "end": 95.0, "text": "первый", "speaker": "SPEAKER_00"},
        {"start": 95.0, "end": 100.0, "text": "второй", "speaker": "SPEAKER_01"},
    ]
    result = drop_marginal_speakers(entries, min_share=0.05)
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]


def test_render_dialogue_with_none_entry_between_speakers_does_not_crash() -> None:
    # merge_entries routinely emits speaker: None for stretches diarization did
    # not cover. A false attribution is worse than an unlabelled line, so this
    # must render as a bare block, never crash, and never corrupt the speakers
    # on either side of it.
    entries = [
        {"start": 0.0, "end": 5.0, "text": "первая фраза", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 9.0, "text": "непонятно кто", "speaker": None},
        {"start": 9.0, "end": 14.0, "text": "вторая фраза", "speaker": "SPEAKER_01"},
    ]
    assert render_transcript(entries, min_share=0.05) == (
        "Голос 1: первая фраза\n\nнепонятно кто\n\nГолос 2: вторая фраза"
    )


def test_render_consecutive_none_entries_merge_into_one_bare_block() -> None:
    entries = [
        {"start": 0.0, "end": 5.0, "text": "привет", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 7.0, "text": "неразборчиво", "speaker": None},
        {"start": 7.0, "end": 9.0, "text": "совсем неразборчиво", "speaker": None},
        {"start": 9.0, "end": 14.0, "text": "нормально", "speaker": "SPEAKER_01"},
    ]
    assert render_transcript(entries, min_share=0.05) == (
        "Голос 1: привет\n\nнеразборчиво совсем неразборчиво\n\nГолос 2: нормально"
    )


def test_render_never_emits_literal_none_label() -> None:
    entries = [
        {"start": 0.0, "end": 5.0, "text": "первая фраза", "speaker": "SPEAKER_00"},
        {"start": 5.0, "end": 9.0, "text": "непонятно кто", "speaker": None},
        {"start": 9.0, "end": 14.0, "text": "вторая фраза", "speaker": "SPEAKER_01"},
    ]
    rendered = render_transcript(entries, min_share=0.05)
    assert "None" not in rendered
