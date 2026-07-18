import random
import re

import pytest

from vts.services.summarizer import split_utterances


def test_split_utterances_splits_on_labels() -> None:
    text = "Голос 1: привет как дела\n\nГолос 2: нормально а у тебя"
    assert split_utterances(text) == [
        "Голос 1: привет как дела",
        "Голос 2: нормально а у тебя",
    ]


def test_split_utterances_without_labels_returns_whole_text() -> None:
    text = "просто сплошной текст без меток"
    assert split_utterances(text) == ["просто сплошной текст без меток"]


def test_split_utterances_keeps_multiline_utterance_together() -> None:
    text = "Голос 1: первая строка\nвторая строка\n\nГолос 2: ответ"
    assert split_utterances(text) == [
        "Голос 1: первая строка\nвторая строка",
        "Голос 2: ответ",
    ]


def test_split_utterances_multidigit_speaker_still_matches() -> None:
    text = "Голос 10: привет\n\nГолос 11: ответ"
    assert split_utterances(text) == ["Голос 10: привет", "Голос 11: ответ"]


def test_split_utterances_does_not_split_on_embedded_single_newline() -> None:
    """render_cleaned_transcript only starts a new label after a blank line
    ("\\n\\n") or at text start — never after a bare "\\n". If the regex
    matches at any line start, an ASR entry whose text happens to contain an
    embedded newline followed by a label-shaped string ("...\\nГолос 2: ...")
    truncates the real utterance and fabricates a wrongly-attributed one.
    """
    text = "он мне сказал\nГолос 2: пока и ушел"
    # The whole text is one utterance from Голос 2 (or unlabeled, depending on
    # the caller's convention) — but crucially, "он мне сказал" must not be
    # silently dropped, and "Голос 2:" must not be fabricated as a new
    # utterance boundary out of a bare newline.
    result = split_utterances(text)
    joined = "".join(result)
    assert "он мне сказал" in joined
    assert result == [text]


def test_split_utterances_recognizes_named_labels() -> None:
    """Registry names replace "Голос N" in the rendered transcript (vts-552),
    so the splitter must treat "Вася:" as a label or a named dialogue collapses
    into one undifferentiated blob."""
    text = "Вася: привет как дела\n\nПетя: нормально а у тебя"
    assert split_utterances(text) == [
        "Вася: привет как дела",
        "Петя: нормально а у тебя",
    ]


def test_split_utterances_mixes_named_and_numbered_labels() -> None:
    """Only some voices get matched to a registry person, so both label shapes
    appear in the same transcript."""
    text = "Вася: привет\n\nГолос 2: кто это\n\nВася: это я"
    assert split_utterances(text) == ["Вася: привет", "Голос 2: кто это", "Вася: это я"]


def test_split_utterances_named_label_with_surname() -> None:
    text = "Иван Петров: доклад начат\n\nГолос 2: спасибо"
    assert split_utterances(text) == ["Иван Петров: доклад начат", "Голос 2: спасибо"]


def test_split_utterances_does_not_split_on_prose_colon() -> None:
    """A blank line followed by a sentence that merely contains a colon is not
    a label — broadening the regex must not fabricate utterance boundaries."""
    text = (
        "Голос 1: вот что я думаю\n\n"
        "Именно поэтому мы решили так: сначала тесты, потом код, и это важно"
    )
    # Current behavior (must not regress): a prose block after a blank line is
    # NOT a label, so it stays attached to the utterance it follows rather than
    # becoming a fabricated speaker named "Именно поэтому мы решили так".
    assert split_utterances(text) == [text]


def test_split_utterances_named_label_not_split_on_embedded_newline() -> None:
    """The blank-line boundary rule must hold for named labels too."""
    text = "он мне сказал\nВася: пока и ушел"
    assert split_utterances(text) == [text]


class _FakeTokenizer:
    """Token == word, which keeps the window arithmetic readable in tests."""

    async def tokenize(self, *, model: str, text: str, tokenizer_path: str | None = None) -> list[int]:
        return list(range(len(text.split())))

    async def detokenize(self, *, model: str, tokens: list[int], tokenizer_path: str | None = None) -> str:
        return " ".join(str(t) for t in tokens)


async def test_chunk_text_utterance_mode_never_splits_an_utterance() -> None:
    from vts.services.summarizer import LLMClient

    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    text = "\n\n".join(f"Голос {i}: слово слово слово слово" for i in (1, 2, 1, 2))
    chunks = await client.chunk_text(
        text=text,
        model="m",
        window_tokens=12,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )

    # Every chunk must contain whole utterances only.
    for chunk in chunks:
        assert chunk.count("Голос") >= 1
        for line in chunk.split("\n\n"):
            assert line.startswith("Голос ")
    # And no utterance may appear twice: overlap is off in utterance mode.
    joined = "\n\n".join(chunks)
    assert joined.count("Голос 1: слово слово слово слово") == 2


async def test_chunk_text_utterance_longer_than_window_repeats_label() -> None:
    from vts.services.summarizer import LLMClient

    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    long_utterance = "Голос 1: " + " ".join(["слово"] * 40)
    chunks = await client.chunk_text(
        text=long_utterance,
        model="m",
        window_tokens=10,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )

    # A 10-minute monologue cannot fit one window; it is cut by tokens, but each
    # continuation must carry the label so attribution survives.
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("Голос 1:")


async def test_chunk_text_utterance_longer_than_window_stays_within_budget() -> None:
    """The regression this task exists to fix: startswith/len checks alone do
    not catch a chunk that re-tokenizes OVER window_tokens. Every returned
    chunk — including the label it carries — must fit the budget the caller
    asked for, because window_tokens is derived from the model's remaining
    context (n_ctx - prompt_tokens); an over-budget chunk overflows the
    request at call time.
    """
    from vts.services.summarizer import LLMClient

    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    long_utterance = "Голос 1: " + " ".join(["слово"] * 40)
    window_tokens = 10
    chunks = await client.chunk_text(
        text=long_utterance,
        model="m",
        window_tokens=window_tokens,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )

    assert len(chunks) > 1
    for chunk in chunks:
        actual = len(await fake.tokenize(model="m", text=chunk))
        assert actual <= window_tokens, f"chunk {chunk!r} is {actual} tokens, budget is {window_tokens}"


async def test_chunk_text_packed_window_stays_within_budget() -> None:
    """Same budget property, for the packed (non-overflow) path."""
    from vts.services.summarizer import LLMClient

    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    text = "\n\n".join(f"Голос {i}: слово слово слово слово" for i in (1, 2, 1, 2))
    window_tokens = 12
    chunks = await client.chunk_text(
        text=text,
        model="m",
        window_tokens=window_tokens,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )
    for chunk in chunks:
        actual = len(await fake.tokenize(model="m", text=chunk))
        assert actual <= window_tokens


async def test_chunk_text_label_at_or_over_window_budget_does_not_hang() -> None:
    """Degenerate case: the label alone meets or exceeds window_tokens.

    Documented behavior: the label is never truncated (a partial label is
    worse than a temporarily over-budget chunk — attribution must stay
    legible), so when label_tokens >= window_tokens each emitted chunk is
    label + exactly one body token, and the loop still terminates instead of
    spinning forever or emitting empty chunks.
    """
    from vts.services.summarizer import LLMClient

    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    long_utterance = "Голос 1: " + " ".join(["слово"] * 10)
    # "Голос 1: " tokenizes (word-split) to 2 tokens; window_tokens == 2 makes
    # the label alone consume the entire budget.
    window_tokens = 2
    chunks = await client.chunk_text(
        text=long_utterance,
        model="m",
        window_tokens=window_tokens,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )
    assert chunks  # terminates, and produces something
    for chunk in chunks:
        assert chunk.strip()
        assert chunk.startswith("Голос 1:")


@pytest.mark.parametrize("trial", range(200))
async def test_chunk_text_utterance_mode_budget_property_fuzz(trial: int) -> None:
    """Fuzz: random utterance lists and window sizes, every chunk must fit.

    Covers windows barely larger than a label, utterances exactly at the
    boundary, single- and multi-utterance inputs. Before the label-budget fix
    this failed on the vast majority of trials that produced a long-utterance
    split; after the fix it must fail on none — EXCEPT the documented
    degenerate case (label_tokens >= window_tokens), which this fuzz
    excludes deliberately: that case is covered on its own by
    test_chunk_text_label_at_or_over_window_budget_does_not_hang, with a
    weaker, documented contract (terminates, label stays intact, body may
    overflow).
    """
    from vts.services.summarizer import LLMClient

    rng = random.Random(trial)
    client = LLMClient(url="http://llama.local/v1")
    fake = _FakeTokenizer()
    client.tokenize = fake.tokenize
    client.detokenize = fake.detokenize

    num_utterances = rng.randint(1, 4)
    speakers = [rng.randint(1, 9) for _ in range(num_utterances)]
    label_tokens_list = [len(f"Голос {speaker}: ".split()) for speaker in speakers]
    max_label_tokens = max(label_tokens_list)
    # Window strictly larger than every label in play, so the non-degenerate
    # budget contract (chunk <= window_tokens, always) applies. Barely larger
    # (label_tokens + 1) through generous (label_tokens + 16).
    window_tokens = rng.randint(max_label_tokens + 1, max_label_tokens + 16)
    utterances = [
        (f"Голос {speaker}: ", rng.randint(0, 60)) for speaker in speakers
    ]

    text = "\n\n".join(
        f"{label}" + " ".join(["слово"] * word_count) if word_count else label.strip()
        for label, word_count in utterances
    )

    chunks = await client.chunk_text(
        text=text,
        model="m",
        window_tokens=window_tokens,
        overlap_ratio=0.15,
        split_on_utterances=True,
    )
    for chunk in chunks:
        actual = len(await fake.tokenize(model="m", text=chunk))
        assert actual <= window_tokens, (
            f"trial={trial} window_tokens={window_tokens} chunk={chunk!r} tokens={actual}"
        )


def test_split_utterances_keeps_a_leading_bare_block() -> None:
    # The renderer emits an unlabelled block for audio diarization never
    # covered, deliberately refusing to attribute it. A LEADING one has no
    # previous block to merge into, so dropping the prefix loses it outright —
    # a meeting opening on music or crosstalk never reaches the summary.
    text = "вступительная фраза без спикера\n\nГолос 1: первый\n\nГолос 2: второй"
    assert split_utterances(text) == [
        "вступительная фраза без спикера",
        "Голос 1: первый",
        "Голос 2: второй",
    ]


def test_split_utterances_conserves_text_from_the_real_renderer() -> None:
    # Draw the input from the actual producer rather than hand-rolled labels:
    # synthetic fixtures kept missing shapes the renderer really emits, which
    # is how the leading-bare-block loss survived a 2000-trial fuzz.
    from vts.services.diarization.merge import drop_marginal_speakers, label_map, render_cleaned_transcript

    shapes = [
        [{"start": 0.0, "end": 5.0, "text": "аноним", "speaker": None},
         {"start": 5.0, "end": 40.0, "text": "первый", "speaker": "SPEAKER_00"},
         {"start": 40.0, "end": 80.0, "text": "второй", "speaker": "SPEAKER_01"}],
        [{"start": 0.0, "end": 40.0, "text": "первый", "speaker": "SPEAKER_00"},
         {"start": 40.0, "end": 45.0, "text": "аноним", "speaker": None},
         {"start": 45.0, "end": 80.0, "text": "второй", "speaker": "SPEAKER_01"}],
        [{"start": 0.0, "end": 40.0, "text": "первый", "speaker": "SPEAKER_00"},
         {"start": 40.0, "end": 80.0, "text": "второй", "speaker": "SPEAKER_01"},
         {"start": 80.0, "end": 85.0, "text": "аноним", "speaker": None}],
    ]
    for entries in shapes:
        cleaned = drop_marginal_speakers(entries, 0.0)
        rendered = render_cleaned_transcript(cleaned, label_map(cleaned))
        assert "\n\n".join(split_utterances(rendered)) == rendered, rendered
