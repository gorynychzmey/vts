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
