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


import json

from vts.pipeline.steps.summarization import participant_vars, render_prompt_vars


def test_participant_vars_json_arrays() -> None:
    v = participant_vars(["Вася", "Петя"], ["Голос 2"])
    assert json.loads(v["NAMED_SPEAKERS"]) == ["Вася", "Петя"]
    assert json.loads(v["ANONYMOUS_SPEAKERS"]) == ["Голос 2"]


def test_participant_vars_empty() -> None:
    v = participant_vars([], [])
    assert v["NAMED_SPEAKERS"] == "[]"
    assert v["ANONYMOUS_SPEAKERS"] == "[]"


def test_participant_vars_does_not_escape_cyrillic() -> None:
    # ensure_ascii=False — the prompt must show "Вася", not "Ва..."
    assert "Вася" in participant_vars(["Вася"], [])["NAMED_SPEAKERS"]


def test_render_prompt_vars_substitutes_participants() -> None:
    prompt = "Участники: ${NAMED_SPEAKERS}. Анонимные: ${ANONYMOUS_SPEAKERS}."
    out = render_prompt_vars(
        prompt, named_speakers=["Вася"], anonymous_speakers=["Голос 2"]
    )
    assert out == 'Участники: ["Вася"]. Анонимные: ["Голос 2"].'


def test_render_prompt_vars_defaults_to_empty_arrays() -> None:
    """An undiarized task substitutes empty arrays rather than leaving raw
    ${NAMED_SPEAKERS} text in the prompt."""
    prompt = "Участники: ${NAMED_SPEAKERS}. Анонимные: ${ANONYMOUS_SPEAKERS}."
    out = render_prompt_vars(prompt)
    assert out == "Участники: []. Анонимные: []."
    assert "${" not in out


def test_rewrite_prompt_still_carries_label_behaviour_instruction() -> None:
    """Regression guard: the participant list says WHO is present; it does not
    replace the behavioural rules (keep labels, never merge speakers, leave
    unlabelled text alone). Both must coexist."""
    result = rewrite_prompt("Rewrite it.", diarized=True)
    assert "Голос 1:" in result
    assert "unlabelled" in result
    assert "never attribute" in result.lower()


def test_shipped_prompts_participant_vars_are_substituted() -> None:
    """Every ${NAMED_SPEAKERS}/${ANONYMOUS_SPEAKERS} placeholder in the shipped
    prompt files must be substituted by render_prompt_vars — an unsubstituted
    placeholder reaches the model as literal '${NAMED_SPEAKERS}' noise."""
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    for name in ("segment_prompt.md", "global_prompt.md"):
        raw = (prompts_dir / name).read_text(encoding="utf-8")
        if "${NAMED_SPEAKERS}" not in raw and "${ANONYMOUS_SPEAKERS}" not in raw:
            continue
        out = render_prompt_vars(raw, named_speakers=["Вася"], anonymous_speakers=[])
        assert "${NAMED_SPEAKERS}" not in out, name
        assert "${ANONYMOUS_SPEAKERS}" not in out, name
        assert '["Вася"]' in out, name
