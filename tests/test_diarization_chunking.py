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
