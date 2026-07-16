from vts.pipeline.steps.summarization import rewrite_prompt


def test_rewrite_prompt_unchanged_without_diarization() -> None:
    base = "Rewrite the transcript segment as clean fluent text."
    # Zero regression: an undiarized task must see the exact original prompt.
    assert rewrite_prompt(base, diarized=False) == base


def test_rewrite_prompt_asks_to_keep_labels_when_diarized() -> None:
    base = "Rewrite the transcript segment as clean fluent text."
    result = rewrite_prompt(base, diarized=True)
    assert base in result
    assert "Голос" in result
    assert len(result) > len(base)


def test_rewrite_prompt_tells_the_model_to_leave_unlabelled_text_alone() -> None:
    # A mid-transcript bare block reaches the model sitting under the previous
    # speaker's label. Without this clause the model attributes it to them —
    # the false claim the renderer refused to make by leaving it bare.
    result = rewrite_prompt("Rewrite it.", diarized=True)
    assert "unlabelled" in result
    assert "never attribute" in result.lower()


def test_rewrite_prompt_quotes_speaker_label_for_english_language() -> None:
    # Finding 1: the instruction must quote the label the renderer actually
    # produced for this recording's language, or it tells the model to keep an
    # example ("Голос 1:") it will never see in an English transcript.
    result = rewrite_prompt("Rewrite it.", diarized=True, language="en")
    assert "Speaker 1:" in result
    assert "Голос" not in result


def test_rewrite_prompt_defaults_to_russian_label_without_language() -> None:
    # Zero regression: callers that omit `language` (as this pre-existing test
    # signature did before language mattered) must keep the original
    # Russian-only instruction.
    result = rewrite_prompt("Rewrite it.", diarized=True)
    assert "Голос 1:" in result
    assert "Speaker" not in result
