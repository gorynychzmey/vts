# Speaker Diarization Implementation Plan (vts-5xz)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Размечать транскрипт по спикерам («Голос N:») и доводить эту разметку до саммари и пользовательских промптов.

**Architecture:** Диаризация живёт в отдельном контейнере за HTTP (как whisper); основное приложение получает только тонкий httpx-клиент. Новый шаг пайплайна `diarize` пишет `outputs/diarization.json`; `MergeTranscriptStep` мёржит спикеров в фразы по словам с деградацией до максимального перекрытия. Метки едут в саммари через `transcript.json → text`, который уже читает `PrepareSummaryChunksStep`.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, httpx, pytest. Контейнер: pyannote.audio 4.x.

**Спека:** `docs/superpowers/specs/2026-07-15-speaker-diarization-design.md` — читать перед началом, там обоснования решений.

## Статус (2026-07-16)

**Задачи 1-10 — DONE.** 997 тестов проходят. Контейнер собран и verified offline.

Остаётся **Task 11** — калибровка на реальной встрече 4+. Пороги в конфиге сейчас
прикидки, и главный открытый вопрос — сохранит ли LLM метки `Голос N:` при
переписывании окон — проверяется только реальным прогоном.

Реализация шла с доработками сверх плана (см. историю коммитов): язык меток
следует языку записи, бюджет окна учитывает токены метки, галлюцинации режутся
на границе предложений. Task 10 разошёлся с планом в трёх местах — подробности
в самой секции Task 10.

## Global Constraints

- **PyTorch НЕ добавляется в `requirements.txt`.** Основное приложение — оркестратор; ML живёт в контейнерах. Нарушение этого правила — провал задачи.
- **Диаризация выключена по умолчанию:** `diarization_enabled_default: bool = False`.
- **Нулевая регрессия:** нет `outputs/diarization.json` → поведение байт-в-байт как сейчас (`entries`, `transcript.txt`, промпт переписывания, нарезка окон с 15% overlap).
- **В `entries` и `diarization.json` — только техническая метка** (`SPEAKER_00`). `Голос N` существует исключительно в рендере `transcript.txt`. Иначе vts-80i не сможет переименовать спикера.
- Пороги в конфиг, не хардкод: `≥2 слов И ≥0.8 сек` (разрез), `<5%` (отсечка спикера).
- Язык кода/комментариев — английский, как в существующем коде. Пользовательские строки (`Голос N`) — русские.
- Тесты: `pytest`. Существующие тесты не ломать.
- `pytest.ini` задаёт `asyncio_mode = auto` — async-тесты пишутся БЕЗ `@pytest.mark.asyncio`.
- `write_json` (`vts/services/storage.py:32`) сам создаёт родительские директории и пишет с `ensure_ascii=True, indent=2` — использовать его, а не `Path.write_text(json.dumps(...))`.

---

## File Structure

**Создаются:**
- `vts/services/diarization/__init__.py` — фабрика `create_diarization_backend`
- `vts/services/diarization/_base.py` — `DiarizationBackend(ABC)` + общий `_post_audio`
- `vts/services/diarization/_pyannote.py` — `PyannoteBackend`
- `vts/services/diarization/merge.py` — чистые функции мёржа (без I/O, без HTTP)
- `vts/pipeline/steps/diarization.py` — `DiarizeStep`
- `docker/diarization/Dockerfile` + `docker/diarization/server.py` — контейнер
- `tests/test_diarization_backends.py`, `tests/test_diarization_merge.py`, `tests/test_diarization_render.py`, `tests/test_diarization_chunking.py`

**Модифицируются:**
- `vts/core/config.py` — настройки диаризации
- `config.yaml` — секция `services.diarization`
- `vts/pipeline/processor.py:64` — создание бэкенда
- `vts/pipeline/context.py:36` — проброс в контекст
- `vts/pipeline/steps/registry.py` — регистрация шага
- `vts/pipeline/steps/transcription.py` — `MergeTranscriptStep` мёржит спикеров
- `vts/services/summarizer.py:401` — нарезка по репликам
- `vts/pipeline/steps/summarization.py` — промпт + вызов нарезки
- `vts/api/schemas.py` — опция `diarize`
- `docker-compose.yml` — сервис `diarization`

**Порядок задач:** 1 (мёрж, чистые функции) → 2 (рендер) → 3 (клиент) → 4 (конфиг+проводка) → 5 (шаг) → 6 (интеграция в merge_transcript) → 7 (нарезка окон) → 8 (промпт) → 9 (API-опция) → 10 (контейнер).

Задачи 1-2 — чистая логика без зависимостей, их можно делать первыми и тестировать без инфраструктуры.

---

## Task 1: Merge logic (pure functions)

Ядро фичи. Никакого I/O — только данные на входе и выходе.

**Files:**
- Create: `vts/services/diarization/merge.py`
- Create: `vts/services/diarization/__init__.py` (пока пустой, наполним в Task 3)
- Test: `tests/test_diarization_merge.py`

**Interfaces:**
- Consumes: ничего
- Produces:
  - `DiarSegment = dict[str, Any]` — `{"start": float, "end": float, "speaker": str}`
  - `speaker_at(diar_segments: list[DiarSegment], start: float, end: float) -> str | None` — спикер с максимальным перекрытием интервала
  - `usable_words(raw_json: dict[str, Any]) -> list[dict[str, Any]] | None` — слова с таймкодами или `None`
  - `split_entry_by_speaker(entry: dict[str, Any], words: list[dict[str, Any]], diar_segments: list[DiarSegment], min_words: int, min_seconds: float) -> list[dict[str, Any]]`
  - `merge_entries(entries: list[dict[str, Any]], raw_json_by_index: dict[int, dict[str, Any]], diar_segments: list[DiarSegment], min_words: int, min_seconds: float) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test for `speaker_at`**

Create `tests/test_diarization_merge.py`:

```python
from vts.services.diarization.merge import speaker_at

DIAR = [
    {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
    {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_01"},
]


def test_speaker_at_fully_inside_one_segment() -> None:
    assert speaker_at(DIAR, 1.0, 5.0) == "SPEAKER_00"


def test_speaker_at_picks_maximum_overlap() -> None:
    # 8.0-12.0: 2s overlap with SPEAKER_00, 2s with SPEAKER_01 — tie goes to the
    # earlier segment, keeping the result deterministic.
    assert speaker_at(DIAR, 8.0, 12.0) == "SPEAKER_00"
    # 9.0-14.0: 1s with SPEAKER_00, 4s with SPEAKER_01
    assert speaker_at(DIAR, 9.0, 14.0) == "SPEAKER_01"


def test_speaker_at_no_overlap_returns_none() -> None:
    assert speaker_at(DIAR, 30.0, 40.0) is None


def test_speaker_at_empty_diarization_returns_none() -> None:
    assert speaker_at([], 1.0, 5.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.services.diarization'`

- [ ] **Step 3: Implement `speaker_at`**

Create `vts/services/diarization/__init__.py` (empty for now — the factory lands in Task 3):

```python
from __future__ import annotations
```

Create `vts/services/diarization/merge.py`:

```python
"""Pure merge helpers: diarization segments -> transcript entries.

No I/O, no HTTP — everything here takes plain data and returns plain data so the
merge rules stay testable without a diarization backend.
"""

from __future__ import annotations

from typing import Any

DiarSegment = dict[str, Any]


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def speaker_at(
    diar_segments: list[DiarSegment],
    start: float,
    end: float,
) -> str | None:
    """Speaker whose diarization segment overlaps [start, end] the most.

    Ties resolve to the earliest segment, so the result never depends on sort
    stability of the caller's input.
    """
    best_speaker: str | None = None
    best_overlap = 0.0
    for segment in diar_segments:
        overlap = _overlap(start, end, float(segment["start"]), float(segment["end"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = str(segment["speaker"])
    return best_speaker
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the failing test for `usable_words`**

whisper.cpp returns subword fragments in `words` (see the existing
`tests/test_transcription_backends.py::test_normalize_output_ignores_subword_tokens`),
so the presence of a `words` key does not mean the words are usable.

Append to `tests/test_diarization_merge.py`:

```python
from vts.services.diarization.merge import usable_words


def test_usable_words_extracts_asr_words() -> None:
    raw = {
        "segments": [
            {
                "words": [
                    {"word": "привет", "start": 0.0, "end": 0.5},
                    {"word": "мир", "start": 0.5, "end": 1.0},
                ]
            }
        ]
    }
    words = usable_words(raw)
    assert words is not None
    assert [w["word"] for w in words] == ["привет", "мир"]


def test_usable_words_none_when_no_words() -> None:
    assert usable_words({"segments": [{"text": "нет слов"}]}) is None
    assert usable_words({}) is None


def test_usable_words_rejects_subword_fragments() -> None:
    # whisper.cpp emits subword tokens; splitting utterances on those would cut
    # words in half, so they must be rejected outright.
    raw = {
        "segments": [
            {
                "words": [
                    {"word": " к", "start": 0.0, "end": 0.1},
                    {"word": "от", "start": 0.1, "end": 0.2},
                    {"word": "ор", "start": 0.2, "end": 0.3},
                    {"word": "ые", "start": 0.3, "end": 0.4},
                ]
            }
        ]
    }
    assert usable_words(raw) is None


def test_usable_words_none_when_timestamps_missing() -> None:
    raw = {"segments": [{"words": [{"word": "привет"}]}]}
    assert usable_words(raw) is None
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'usable_words'`

- [ ] **Step 7: Implement `usable_words`**

Append to `vts/services/diarization/merge.py`:

```python
# whisper.cpp emits subword tokens in `words` ("к", "от", "ор", "ые"), which are
# useless for splitting utterances. Real words are longer and mostly not glued
# fragments; a corpus where most tokens are 1-2 chars is a tokenizer artifact,
# not speech.
_SUBWORD_MAX_LEN = 2
_SUBWORD_RATIO = 0.5


def usable_words(raw_json: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Word-level timestamps from a Whisper payload, or None when unusable.

    Returns None when the backend gave no words, gave words without timestamps,
    or gave subword fragments (whisper.cpp). Callers fall back to whole-entry
    attribution in that case.
    """
    segments = raw_json.get("segments")
    if not isinstance(segments, list):
        return None

    words: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            if word.get("start") is None or word.get("end") is None:
                return None
            words.append(word)

    if not words:
        return None

    short = sum(1 for w in words if len(str(w.get("word", "")).strip()) <= _SUBWORD_MAX_LEN)
    if short / len(words) > _SUBWORD_RATIO:
        return None
    return words
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: PASS (8 tests)

- [ ] **Step 9: Commit**

```bash
git add vts/services/diarization/ tests/test_diarization_merge.py
git commit -m "feat(diarization): overlap-based speaker lookup and word usability check (vts-5xz)"
```

- [ ] **Step 10: Write the failing test for `split_entry_by_speaker`**

This is the heart of the feature — the ≥2 words AND ≥0.8s threshold from the spec.

Append to `tests/test_diarization_merge.py`:

```python
from vts.services.diarization.merge import split_entry_by_speaker

TWO_SPEAKERS = [
    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
]


def test_split_entry_single_speaker_stays_one_entry() -> None:
    entry = {"start": 0.0, "end": 3.0, "text": "да я согласен полностью"}
    words = [
        {"word": "да", "start": 0.0, "end": 0.8},
        {"word": "я", "start": 0.8, "end": 1.4},
        {"word": "согласен", "start": 1.4, "end": 2.2},
        {"word": "полностью", "start": 2.2, "end": 3.0},
    ]
    result = split_entry_by_speaker(entry, words, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "да я согласен полностью"


def test_split_entry_real_turn_change_splits() -> None:
    # "да согласен" (SPEAKER_00, 0-4s) then "а ты что думаешь" (SPEAKER_01, 5-9s)
    entry = {"start": 0.0, "end": 9.0, "text": "да согласен а ты что думаешь"}
    words = [
        {"word": "да", "start": 0.0, "end": 2.0},
        {"word": "согласен", "start": 2.0, "end": 4.0},
        {"word": "а", "start": 5.0, "end": 6.0},
        {"word": "ты", "start": 6.0, "end": 7.0},
        {"word": "что", "start": 7.0, "end": 8.0},
        {"word": "думаешь", "start": 8.0, "end": 9.0},
    ]
    result = split_entry_by_speaker(entry, words, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 2
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "да согласен"
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 4.0
    assert result[1]["speaker"] == "SPEAKER_01"
    assert result[1]["text"] == "а ты что думаешь"
    assert result[1]["start"] == 5.0
    assert result[1]["end"] == 9.0


def test_split_entry_short_backchannel_absorbed() -> None:
    # "угу" from SPEAKER_01 mid-sentence: 1 word, 0.3s — below both thresholds,
    # so it must be absorbed by the PREVIOUS group and produce no entry.
    diar = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 2.3, "speaker": "SPEAKER_01"},
        {"start": 2.3, "end": 5.0, "speaker": "SPEAKER_00"},
    ]
    entry = {"start": 0.0, "end": 5.0, "text": "я думаю угу что это верно"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "угу", "start": 2.0, "end": 2.3},
        {"word": "что", "start": 2.3, "end": 3.0},
        {"word": "это", "start": 3.0, "end": 4.0},
        {"word": "верно", "start": 4.0, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "я думаю угу что это верно"


def test_split_entry_group_below_word_threshold_absorbed() -> None:
    # A group that is long enough in seconds but only 1 word fails the AND.
    diar = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
    ]
    entry = {"start": 0.0, "end": 4.0, "text": "я думаю дааааа"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "дааааа", "start": 2.0, "end": 4.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"


def test_split_entry_first_group_below_threshold_absorbed_forward() -> None:
    # No previous group to absorb into — the leading fragment joins the next one.
    diar = [
        {"start": 0.0, "end": 0.3, "speaker": "SPEAKER_01"},
        {"start": 0.3, "end": 5.0, "speaker": "SPEAKER_00"},
    ]
    entry = {"start": 0.0, "end": 5.0, "text": "угу я думаю что это верно"}
    words = [
        {"word": "угу", "start": 0.0, "end": 0.3},
        {"word": "я", "start": 0.3, "end": 1.5},
        {"word": "думаю", "start": 1.5, "end": 3.0},
        {"word": "что", "start": 3.0, "end": 4.0},
        {"word": "это", "start": 4.0, "end": 4.5},
        {"word": "верно", "start": 4.5, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "угу я думаю что это верно"


def test_split_entry_unattributed_words_join_previous() -> None:
    # Words outside any diarization segment must not vanish.
    diar = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}]
    entry = {"start": 0.0, "end": 5.0, "text": "я думаю что это верно"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "что", "start": 3.0, "end": 3.5},
        {"word": "это", "start": 3.5, "end": 4.0},
        {"word": "верно", "start": 4.0, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["text"] == "я думаю что это верно"
    assert result[0]["speaker"] == "SPEAKER_00"
```

- [ ] **Step 11: Run test to verify it fails**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'split_entry_by_speaker'`

- [ ] **Step 12: Implement `split_entry_by_speaker`**

Append to `vts/services/diarization/merge.py`:

```python
def _group_words_by_speaker(
    words: list[dict[str, Any]],
    diar_segments: list[DiarSegment],
) -> list[dict[str, Any]]:
    """Consecutive words sharing a speaker, collapsed into groups.

    Words that overlap no diarization segment inherit the running speaker, so a
    gap in diarization never drops text.
    """
    groups: list[dict[str, Any]] = []
    for word in words:
        start = float(word["start"])
        end = float(word["end"])
        speaker = speaker_at(diar_segments, start, end)
        if groups and (speaker is None or groups[-1]["speaker"] == speaker):
            groups[-1]["words"].append(word)
            groups[-1]["end"] = end
            continue
        groups.append({"speaker": speaker, "words": [word], "start": start, "end": end})
    return groups


def _absorb_small_groups(
    groups: list[dict[str, Any]],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Fold groups below the thresholds into their neighbour.

    Short backchannels ("угу") are exactly where diarization is least reliable,
    so splitting on them trades readable text for a low-confidence signal. They
    merge into the PREVIOUS group, which keeps the text in speaking order; a
    leading fragment has no previous group and folds forward instead.
    """
    kept: list[dict[str, Any]] = []
    for group in groups:
        big_enough = (
            len(group["words"]) >= min_words
            and (group["end"] - group["start"]) >= min_seconds
        )
        if big_enough or group["speaker"] is None:
            if group["speaker"] is None and kept:
                kept[-1]["words"].extend(group["words"])
                kept[-1]["end"] = group["end"]
                continue
            kept.append(group)
            continue
        if kept:
            kept[-1]["words"].extend(group["words"])
            kept[-1]["end"] = group["end"]
        else:
            kept.append(group)
    return _merge_adjacent_same_speaker(kept)


def _merge_adjacent_same_speaker(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        if merged and merged[-1]["speaker"] == group["speaker"]:
            merged[-1]["words"].extend(group["words"])
            merged[-1]["end"] = group["end"]
            continue
        merged.append(group)
    return merged


def _group_text(group: dict[str, Any]) -> str:
    return " ".join(str(w.get("word", "")).strip() for w in group["words"] if str(w.get("word", "")).strip())


def split_entry_by_speaker(
    entry: dict[str, Any],
    words: list[dict[str, Any]],
    diar_segments: list[DiarSegment],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Split one transcript entry where the speaker genuinely changes.

    A group becomes its own entry only when it clears BOTH thresholds; smaller
    groups are absorbed. When nothing clears them, the entry stays whole and is
    attributed by maximum overlap.
    """
    if not words:
        speaker = speaker_at(diar_segments, float(entry["start"]), float(entry["end"]))
        return [{**entry, "speaker": speaker}]

    groups = _absorb_small_groups(
        _group_words_by_speaker(words, diar_segments),
        min_words,
        min_seconds,
    )
    if len(groups) <= 1:
        speaker = groups[0]["speaker"] if groups else None
        if speaker is None:
            speaker = speaker_at(diar_segments, float(entry["start"]), float(entry["end"]))
        return [{**entry, "speaker": speaker}]

    return [
        {
            "start": group["start"],
            "end": group["end"],
            "text": _group_text(group),
            "speaker": group["speaker"],
        }
        for group in groups
    ]
```

- [ ] **Step 13: Run test to verify it passes**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: PASS (14 tests)

- [ ] **Step 14: Write the failing test for `merge_entries`**

Append to `tests/test_diarization_merge.py`:

```python
from vts.services.diarization.merge import merge_entries


def test_merge_entries_without_words_uses_max_overlap() -> None:
    # The cpp path: no usable words, so each entry gets one speaker as a whole.
    entries = [
        {"start": 0.0, "end": 4.0, "text": "первая фраза"},
        {"start": 6.0, "end": 9.0, "text": "вторая фраза"},
    ]
    result = merge_entries(entries, {}, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert [e["text"] for e in result] == ["первая фраза", "вторая фраза"]


def test_merge_entries_splits_using_words() -> None:
    entries = [{"start": 0.0, "end": 9.0, "text": "да согласен а ты что думаешь"}]
    raw_by_index = {
        0: {
            "segments": [
                {
                    "words": [
                        {"word": "да", "start": 0.0, "end": 2.0},
                        {"word": "согласен", "start": 2.0, "end": 4.0},
                        {"word": "а", "start": 5.0, "end": 6.0},
                        {"word": "ты", "start": 6.0, "end": 7.0},
                        {"word": "что", "start": 7.0, "end": 8.0},
                        {"word": "думаешь", "start": 8.0, "end": 9.0},
                    ]
                }
            ]
        }
    }
    result = merge_entries(entries, raw_by_index, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 2
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]


def test_merge_entries_empty_diarization_leaves_speaker_none() -> None:
    entries = [{"start": 0.0, "end": 4.0, "text": "фраза"}]
    result = merge_entries(entries, {}, [], min_words=2, min_seconds=0.8)
    assert result[0]["speaker"] is None
```

- [ ] **Step 15: Run test to verify it fails**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_entries'`

- [ ] **Step 16: Implement `merge_entries`**

Append to `vts/services/diarization/merge.py`:

```python
def merge_entries(
    entries: list[dict[str, Any]],
    raw_json_by_index: dict[int, dict[str, Any]],
    diar_segments: list[DiarSegment],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Attribute every transcript entry to a speaker.

    Two levels of precision, not two algorithms: entries whose chunk carried
    usable word timestamps get split on genuine turn changes; the rest fall back
    to whole-entry maximum overlap.

    `raw_json_by_index` maps an entry's source chunk index to that chunk's
    Whisper payload. Entries with no matching payload take the fallback path.
    """
    merged: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        raw = raw_json_by_index.get(index)
        words = usable_words(raw) if isinstance(raw, dict) else None
        entry_words = (
            [
                word
                for word in words
                if float(word["end"]) > float(entry["start"])
                and float(word["start"]) < float(entry["end"])
            ]
            if words
            else []
        )
        merged.extend(
            split_entry_by_speaker(entry, entry_words, diar_segments, min_words, min_seconds)
        )
    return merged
```

- [ ] **Step 17: Run test to verify it passes**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: PASS (17 tests)

- [ ] **Step 18: Commit**

```bash
git add vts/services/diarization/merge.py tests/test_diarization_merge.py
git commit -m "feat(diarization): merge speakers into transcript entries (vts-5xz)"
```

---

## Task 2: Render — mode selection and labels

**Files:**
- Modify: `vts/services/diarization/merge.py`
- Test: `tests/test_diarization_render.py`

**Interfaces:**
- Consumes: `merge_entries` output (entries with `speaker`)
- Produces:
  - `drop_marginal_speakers(entries: list[dict[str, Any]], min_share: float) -> list[dict[str, Any]]`
  - `label_map(entries: list[dict[str, Any]]) -> dict[str, str]` — `SPEAKER_00 -> "Голос 1"` по первому появлению
  - `render_transcript(entries: list[dict[str, Any]], min_share: float) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_render.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'drop_marginal_speakers'`

- [ ] **Step 3: Implement render helpers**

Append to `vts/services/diarization/merge.py`:

```python
def drop_marginal_speakers(
    entries: list[dict[str, Any]],
    min_share: float,
) -> list[dict[str, Any]]:
    """Reassign speakers holding a negligible share of speech.

    Diarization invents phantom speakers on music, echo and noise. Such a
    phantom would flip a monologue into a two-voice dialogue, so anything below
    `min_share` of total speech time is folded into the dominant speaker.
    """
    totals: dict[str, float] = {}
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker is None:
            continue
        totals[speaker] = totals.get(speaker, 0.0) + (float(entry["end"]) - float(entry["start"]))

    if not totals:
        return list(entries)

    overall = sum(totals.values())
    if overall <= 0:
        return list(entries)

    dominant = max(totals, key=lambda key: totals[key])
    marginal = {speaker for speaker, total in totals.items() if (total / overall) < min_share}
    if not marginal:
        return list(entries)

    return [
        {**entry, "speaker": dominant} if entry.get("speaker") in marginal else dict(entry)
        for entry in entries
    ]


def label_map(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Technical tags -> "Голос N", numbered by first appearance.

    "Голос 1" is whoever spoke first, which is what a reader expects. The
    technical tag stays in the data; this mapping exists only for rendering.
    """
    mapping: dict[str, str] = {}
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker is None or speaker in mapping:
            continue
        mapping[speaker] = f"Голос {len(mapping) + 1}"
    return mapping


def render_transcript(entries: list[dict[str, Any]], min_share: float) -> str:
    """Flat text for a monologue, labelled turns for a dialogue."""
    cleaned = drop_marginal_speakers(entries, min_share)
    mapping = label_map(cleaned)

    if len(mapping) <= 1:
        return " ".join(str(entry["text"]).strip() for entry in cleaned if str(entry["text"]).strip())

    blocks: list[str] = []
    current: str | None = None
    for entry in cleaned:
        text = str(entry["text"]).strip()
        if not text:
            continue
        speaker = entry.get("speaker")
        if speaker != current:
            blocks.append(f"{mapping[speaker]}: {text}")
            current = speaker
            continue
        blocks[-1] = blocks[-1] + " " + text
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diarization_render.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add vts/services/diarization/merge.py tests/test_diarization_render.py
git commit -m "feat(diarization): render monologue or labelled dialogue (vts-5xz)"
```

---

## Task 3: HTTP client

Mirrors `vts/services/transcription/` exactly — read those three files first.

**Files:**
- Create: `vts/services/diarization/_base.py`
- Create: `vts/services/diarization/_pyannote.py`
- Modify: `vts/services/diarization/__init__.py`
- Test: `tests/test_diarization_backends.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DiarizationBackend(ABC)` with `backend_name: str`, `async diarize(audio_path: Path, timeout_seconds: int = 1800) -> dict[str, Any]`, `normalize_output(payload: dict[str, Any]) -> dict[str, Any]`
  - `PyannoteBackend(DiarizationBackend)`
  - `create_diarization_backend(diarization_url: str, diarization_backend: str) -> DiarizationBackend`

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_backends.py`:

```python
import pytest

from vts.services.diarization import create_diarization_backend
from vts.services.diarization._pyannote import PyannoteBackend


def _pyannote() -> PyannoteBackend:
    return PyannoteBackend("http://localhost")


def test_create_backend_returns_pyannote() -> None:
    backend = create_diarization_backend("http://localhost", "pyannote")
    assert isinstance(backend, PyannoteBackend)
    assert backend.backend_name == "pyannote"


def test_create_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown diarization backend"):
        create_diarization_backend("http://localhost", "nope")


def test_normalize_output_extracts_segments_and_embeddings() -> None:
    payload = {
        "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
        "embeddings": {"SPEAKER_00": [0.1, 0.2]},
        "num_speakers": 1,
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    assert result["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}
    assert result["num_speakers"] == 1


def test_normalize_output_defaults_when_fields_missing() -> None:
    result = _pyannote().normalize_output({})
    assert result == {"segments": [], "embeddings": {}, "num_speakers": 0}


def test_normalize_output_drops_malformed_segments() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "speaker": "SPEAKER_01"},
            "garbage",
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]


def test_normalize_output_infers_num_speakers() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
        ]
    }
    assert _pyannote().normalize_output(payload)["num_speakers"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_backends.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_diarization_backend'`

- [ ] **Step 3: Implement the base**

Create `vts/services/diarization/_base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx


class DiarizationBackend(ABC):
    backend_name: str

    def __init__(self, diarization_url: str) -> None:
        self._url = diarization_url.rstrip("/")

    @abstractmethod
    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]: ...

    def normalize_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Canonical shape: {"segments": [...], "embeddings": {...}, "num_speakers": int}.

        Malformed segments are dropped rather than raising: a partial
        diarization still beats failing a whole task over one bad span.
        """
        segments: list[dict[str, Any]] = []
        for segment in payload.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            if segment.get("start") is None or segment.get("end") is None:
                continue
            if not segment.get("speaker"):
                continue
            segments.append(
                {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "speaker": str(segment["speaker"]),
                }
            )

        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, dict):
            embeddings = {}

        num_speakers = payload.get("num_speakers")
        if not isinstance(num_speakers, int):
            num_speakers = len({segment["speaker"] for segment in segments})

        return {"segments": segments, "embeddings": embeddings, "num_speakers": num_speakers}

    async def _post_audio(
        self,
        endpoint: str,
        audio_path: Path,
        file_key: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        timeout_seconds: int,
        error_context: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            with audio_path.open("rb") as file_obj:
                files = {file_key: (audio_path.name, file_obj, "audio/wav")}
                response = await client.post(endpoint, params=params, data=data, files=files)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid {error_context} response type")
        return payload
```

- [ ] **Step 4: Implement the pyannote backend**

Create `vts/services/diarization/_pyannote.py`:

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._base import DiarizationBackend

_log = logging.getLogger(__name__)


class PyannoteBackend(DiarizationBackend):
    backend_name = "pyannote"

    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        payload = await self._post_audio(
            self._url + "/diarize",
            audio_path,
            "file",
            timeout_seconds=timeout_seconds,
            error_context="pyannote",
        )
        return self.normalize_output(payload)
```

- [ ] **Step 5: Implement the factory**

Replace `vts/services/diarization/__init__.py`:

```python
from __future__ import annotations

from ._base import DiarizationBackend
from ._pyannote import PyannoteBackend


def create_diarization_backend(diarization_url: str, diarization_backend: str) -> DiarizationBackend:
    if diarization_backend == "pyannote":
        return PyannoteBackend(diarization_url)
    raise ValueError(f"Unknown diarization backend: {diarization_backend!r}. Expected 'pyannote'.")


__all__ = [
    "DiarizationBackend",
    "PyannoteBackend",
    "create_diarization_backend",
]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_diarization_backends.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the merge tests to confirm the package import still works**

Run: `pytest tests/test_diarization_merge.py tests/test_diarization_render.py -v`
Expected: PASS (24 tests)

- [ ] **Step 8: Commit**

```bash
git add vts/services/diarization/ tests/test_diarization_backends.py
git commit -m "feat(diarization): pyannote HTTP client (vts-5xz)"
```

---

## Task 4: Config and wiring

**Files:**
- Modify: `vts/core/config.py:92-93` (add after `whisper_backend`), `vts/core/config.py:400-401` (aliases)
- Modify: `config.yaml:14-16` (after the `whisper` block)
- Modify: `vts/pipeline/processor.py:30,64`
- Modify: `vts/pipeline/context.py:36`
- Test: `tests/test_config_yaml.py`

**Interfaces:**
- Consumes: `create_diarization_backend` (Task 3)
- Produces:
  - `settings.diarization_url: str`, `settings.diarization_backend: str`, `settings.diarization_enabled_default: bool`
  - `settings.diarization_min_words: int`, `settings.diarization_min_seconds: float`, `settings.diarization_min_speaker_share: float`
  - `ctx.diarization: DiarizationBackend`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_yaml.py`:

```python
def test_diarization_defaults() -> None:
    from vts.core.config import Settings

    settings = Settings()
    assert settings.diarization_url == "http://diarization:9100"
    assert settings.diarization_backend == "pyannote"
    # Off by default: a new feature on uncalibrated thresholds plus an extra
    # pass over the audio. Turning it on later is easy; the reverse is not.
    assert settings.diarization_enabled_default is False
    assert settings.diarization_min_words == 2
    assert settings.diarization_min_seconds == 0.8
    assert settings.diarization_min_speaker_share == 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_yaml.py::test_diarization_defaults -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'diarization_url'`

- [ ] **Step 3: Add settings**

In `vts/core/config.py`, immediately after the `whisper_backend: str = "asr"` line (:93):

```python
    diarization_url: str = "http://diarization:9100"
    diarization_backend: str = "pyannote"
    # Diarization stays opt-in: it costs a full extra pass over the audio and
    # gives nothing for single-voice recordings.
    diarization_enabled_default: bool = False
    # An utterance splits only when a speaker group clears BOTH thresholds.
    # Short backchannels ("угу") are where diarization is least reliable, so
    # splitting on them trades readable text for a low-confidence signal.
    diarization_min_words: int = 2
    diarization_min_seconds: float = 0.8
    # Speakers below this share of total speech are phantoms from music or echo.
    diarization_min_speaker_share: float = 0.05
```

In the `services_aliases` dict (:400), after `"services_whisper_backend": "whisper_backend",`:

```python
        "services_diarization_url": "diarization_url",
        "services_diarization_backend": "diarization_backend",
        "services_diarization_enabled_default": "diarization_enabled_default",
        "services_diarization_min_words": "diarization_min_words",
        "services_diarization_min_seconds": "diarization_min_seconds",
        "services_diarization_min_speaker_share": "diarization_min_speaker_share",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_yaml.py::test_diarization_defaults -v`
Expected: PASS

- [ ] **Step 5: Add the config.yaml section**

In `config.yaml`, after the `whisper:` block (:14-16):

```yaml
  diarization:
    url: http://diarization:9100
    backend: pyannote
    # Off by default; enable per task with the `diarize` option.
    enabled_default: false
    # Split an utterance only when a speaker group clears BOTH thresholds.
    min_words: 2
    min_seconds: 0.8
    # Speakers below this share of speech are noise, not people.
    min_speaker_share: 0.05
```

- [ ] **Step 6: Wire the backend into the processor**

In `vts/pipeline/processor.py`, extend the import at :30:

```python
from vts.services.diarization import DiarizationBackend, create_diarization_backend
```

After the `self.whisper` line (:64):

```python
        self.diarization: DiarizationBackend = create_diarization_backend(
            settings.diarization_url, settings.diarization_backend
        )
```

- [ ] **Step 7: Expose it on the context**

In `vts/pipeline/context.py`, after the `self.whisper = proc.whisper` line (:36):

```python
        self.diarization = proc.diarization
```

- [ ] **Step 8: Run the full test suite**

Run: `pytest tests/ -q`
Expected: PASS, no regressions

- [ ] **Step 9: Commit**

```bash
git add vts/core/config.py config.yaml vts/pipeline/processor.py vts/pipeline/context.py tests/test_config_yaml.py
git commit -m "feat(diarization): config and backend wiring (vts-5xz)"
```

---

## Task 5: Pipeline step

**Files:**
- Create: `vts/pipeline/steps/diarization.py`
- Modify: `vts/pipeline/steps/registry.py`
- Test: `tests/test_diarization_step.py`

**Interfaces:**
- Consumes: `ctx.diarization` (Task 4), `ctx.transcribe_audio_path(dirs)` (existing, `vts/pipeline/context.py:199`)
- Produces: `DiarizeStep` with `name = "diarize"`, writes `outputs/diarization.json`

**Critical:** diarize the WHOLE audio via `ctx.transcribe_audio_path(st.dirs)` — never the per-chunk WAVs from `SegmentAudioStep`. Chunks are cut by duration for parallel transcription; the same person in two chunks would get two different tags. That helper also handles `audio_16k.wav` vs `audio_16k_trimmed.wav`, since `TrimInitialSilenceStep` deletes the untrimmed file (`vts/pipeline/steps/media.py:211`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_step.py`:

```python
import json
from pathlib import Path
from types import SimpleNamespace

from vts.pipeline.steps.diarization import DiarizeStep


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def diarize(self, audio_path: Path, timeout_seconds: int = 1800) -> dict:
        self.calls.append(audio_path)
        return {
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
            "embeddings": {"SPEAKER_00": [0.1, 0.2]},
            "num_speakers": 1,
        }


def _dirs(tmp_path: Path) -> dict[str, Path]:
    for name in ("media", "outputs", "segments", "logs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return {name: tmp_path / name for name in ("media", "outputs", "segments", "logs")}


def _ctx(backend: _FakeBackend) -> SimpleNamespace:
    def transcribe_audio_path(dirs: dict[str, Path]) -> Path:
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        return trimmed if trimmed.exists() else dirs["media"] / "audio_16k.wav"

    return SimpleNamespace(diarization=backend, transcribe_audio_path=transcribe_audio_path)


def _state(tmp_path: Path, dirs: dict[str, Path], options: dict) -> SimpleNamespace:
    import logging
    import uuid

    return SimpleNamespace(
        task_id=uuid.uuid4(),
        user_id="user",
        dirs=dirs,
        logger=logging.getLogger("test"),
        task_options=options,
    )


async def test_step_skipped_when_diarize_disabled(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": False}))

    assert backend.calls == []
    assert not (dirs["outputs"] / "diarization.json").exists()


async def test_step_writes_diarization_json(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    payload = json.loads((dirs["outputs"] / "diarization.json").read_text(encoding="utf-8"))
    assert payload["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    # Embeddings ship even though this task never reads them: pyannote returns
    # them for free, and vts-80i would otherwise re-process the whole audio.
    assert payload["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}
    assert payload["num_speakers"] == 1


async def test_step_diarizes_trimmed_audio_when_present(tmp_path: Path) -> None:
    # TrimInitialSilenceStep deletes audio_16k.wav, so the trimmed file is the
    # only one left — diarizing the missing original would crash the task.
    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k_trimmed.wav").write_bytes(b"RIFF")
    backend = _FakeBackend()

    await DiarizeStep().run(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert backend.calls == [dirs["media"] / "audio_16k_trimmed.wav"]


async def test_step_already_done_when_artifact_exists(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["outputs"] / "diarization.json").write_text("{}", encoding="utf-8")
    backend = _FakeBackend()

    done = await DiarizeStep().already_done(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert done is True


async def test_step_already_done_false_when_enabled_and_missing(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    backend = _FakeBackend()

    done = await DiarizeStep().already_done(_ctx(backend), _state(tmp_path, dirs, {"diarize": True}))

    assert done is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_step.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.pipeline.steps.diarization'`

- [ ] **Step 3: Implement the step**

Create `vts/pipeline/steps/diarization.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from vts.pipeline.steps.base import Step, StepState
from vts.services.storage import write_json

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


def diarize_enabled(task_options: dict, default: bool) -> bool:
    """Per-task `diarize`, falling back to the configured default."""
    value = task_options.get("diarize")
    if value is None:
        return default
    return bool(value)


class DiarizeStep(Step):
    name = "diarize"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return (st.dirs["outputs"] / "diarization.json").exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        default = bool(getattr(ctx.settings, "diarization_enabled_default", False))
        if not diarize_enabled(st.task_options, default):
            st.logger.info("diarization skipped: disabled for this task")
            return True

        output = st.dirs["outputs"] / "diarization.json"
        if output.exists():
            return True

        # The whole audio, never the per-chunk WAVs: chunks are cut by duration
        # for parallel transcription, so the same person in two chunks would get
        # two different speaker tags.
        audio_path = ctx.transcribe_audio_path(st.dirs)
        if not audio_path.exists():
            raise RuntimeError(f"Missing audio for diarization: {audio_path}")

        payload = await ctx.diarization.diarize(audio_path=audio_path)

        # We sent audio and got no speakers back. This is NOT what a monologue
        # looks like — a real single-speaker result is one segment spanning the
        # audio, never zero. So this means the sidecar failed or returned
        # something unparseable, and normalize_output degraded it to empty.
        # Writing the artifact anyway would render flat text: a broken sidecar
        # would be indistinguishable from a genuine monologue — wrong, but not
        # obviously wrong, which is the worst failure shape to ship.
        if not payload.get("segments"):
            raise RuntimeError(
                "Diarization returned no speaker segments; refusing to write an "
                "empty artifact that would silently render as a monologue"
            )

        write_json(output, payload)
        st.logger.info("diarization finished: speakers=%s", payload.get("num_speakers"))
        return True
```

**Why this raises rather than degrading:** the client's `normalize_output` deliberately drops malformed segments instead of raising, because a partial diarization beats failing a whole task over one bad span. That is right for a *normalizer* — it has no task context and cannot know whether emptiness is fatal. The step does have that context, so the policy lives here. The pipeline already fails a task loudly when a required artifact cannot be produced; diarization is opt-in, so a user who asked for it should learn it failed rather than silently receive an unlabelled transcript.

Add this test to `tests/test_diarization_step.py`:

```python
async def test_step_raises_when_no_segments_returned(tmp_path: Path) -> None:
    # A broken sidecar degrades to {"segments": [], ...}. Writing that would
    # render flat text — indistinguishable from a real monologue.
    class _EmptyBackend:
        async def diarize(self, audio_path: Path, timeout_seconds: int = 1800) -> dict:
            return {"segments": [], "embeddings": {}, "num_speakers": 0}

    dirs = _dirs(tmp_path)
    (dirs["media"] / "audio_16k.wav").write_bytes(b"RIFF")
    ctx = _ctx(_EmptyBackend())

    with pytest.raises(RuntimeError, match="no speaker segments"):
        await DiarizeStep().run(ctx, _state(tmp_path, dirs, {"diarize": True}))

    assert not (dirs["outputs"] / "diarization.json").exists()
```

Note this test needs `import pytest` in the file.

- [ ] **Step 4: Run test to verify it fails on settings**

Run: `pytest tests/test_diarization_step.py -v`
Expected: FAIL — the fake ctx has no `settings`. Fix the test helper by adding `settings` to `_ctx`:

```python
def _ctx(backend: _FakeBackend) -> SimpleNamespace:
    def transcribe_audio_path(dirs: dict[str, Path]) -> Path:
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        return trimmed if trimmed.exists() else dirs["media"] / "audio_16k.wav"

    return SimpleNamespace(
        diarization=backend,
        transcribe_audio_path=transcribe_audio_path,
        settings=SimpleNamespace(diarization_enabled_default=False),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_diarization_step.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Register the step**

In `vts/pipeline/steps/registry.py`, add the import after the media imports:

```python
from vts.pipeline.steps.diarization import DiarizeStep
```

And in `STEP_REGISTRY`, between `TranscribeSegmentsStep` and `MergeTranscriptStep`:

```python
    DiarizeStep.name: DiarizeStep(),
```

- [ ] **Step 6b: Add it to the DAG — without this the step never runs**

`STEP_REGISTRY` only maps a name to an instance. The list of steps a task actually
executes is `DAG_HEAD` (`vts/pipeline/types.py:8`): `processor.py:150` iterates
`build_dag_steps(task_options)`, which returns `DAG_HEAD + tail`, and that is the
only thing that calls `resolve_step`. A step absent from `DAG_HEAD` is registered,
testable, and dead — unit tests that call `DiarizeStep().run(...)` directly pass
regardless, because they bypass the DAG.

In `vts/pipeline/types.py`, insert `"diarize"` into `DAG_HEAD` between
`transcribe_segments` and `merge_transcript`, matching the registry order:

```python
DAG_HEAD: Final[list[str]] = [
    "download",
    "extract_audio",
    "trim_initial_silence",
    "segment_audio",
    "detect_language",
    "transcribe_segments",
    "diarize",
    "merge_transcript",
    "prepare_llama_model",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
]
```

The step is in the DAG for every task; it decides for itself whether to act,
returning early when `diarize` is off. That matches how the pipeline already
treats conditional work, and keeps the step list static rather than making the
DAG shape depend on options.

Add a test to `tests/test_diarization_step.py` pinning that the step is reachable:

```python
def test_diarize_is_in_the_dag_between_transcription_and_merge() -> None:
    # STEP_REGISTRY only maps names to instances; DAG_HEAD is what a task runs.
    # Without this the step is registered, tested, and never invoked.
    from vts.pipeline.types import DAG_HEAD

    assert "diarize" in DAG_HEAD
    # Order matters: diarization needs the transcript's chunks already done, and
    # merge_transcript consumes the artifact this step writes.
    assert DAG_HEAD.index("transcribe_segments") < DAG_HEAD.index("diarize")
    assert DAG_HEAD.index("diarize") < DAG_HEAD.index("merge_transcript")


def test_diarize_resolves_from_the_registry() -> None:
    from vts.pipeline.steps.registry import resolve_step

    assert isinstance(resolve_step("diarize"), DiarizeStep)
```

**Check the step-weights fallout:** `DAG_HEAD` feeds progress weighting. Grep for
consumers that enumerate it (`grep -rn "DAG_HEAD\|DAG_STEPS" --include="*.py" vts/ tests/`)
and confirm a new member does not break them — `tests/test_step_weights*.py` and
`tests/test_dag_tail.py` are the ones to watch.

- [ ] **Step 7: Run the full test suite**

Run: `pytest tests/ -q`
Expected: PASS, no regressions

- [ ] **Step 8: Commit**

```bash
git add vts/pipeline/steps/diarization.py vts/pipeline/steps/registry.py tests/test_diarization_step.py
git commit -m "feat(diarization): pipeline step over the whole audio (vts-5xz)"
```

---

## Task 6: Wire the merge into MergeTranscriptStep

**Files:**
- Modify: `vts/pipeline/steps/transcription.py` (`MergeTranscriptStep.run`)
- Test: `tests/test_diarization_transcript.py`

**Interfaces:**
- Consumes: `merge_entries`, `render_transcript` (Tasks 1-2); `outputs/diarization.json` (Task 5)
- Produces: `transcript.json` entries carrying `speaker`; `transcript.txt` carrying `Голос N:`

**Critical:** no `diarization.json` → behave byte-for-byte as today.

- [ ] **Step 1: Write the failing regression test**

Create `tests/test_diarization_transcript.py`:

```python
import json
from pathlib import Path

from vts.pipeline.steps.transcription import apply_diarization


def test_no_diarization_file_leaves_entries_untouched(tmp_path: Path) -> None:
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text = apply_diarization(
        entries,
        {},
        tmp_path / "missing.json",
        min_words=2,
        min_seconds=0.8,
        min_share=0.05,
    )
    # Zero regression: same entries, no speaker key, text joined as before.
    assert result == entries
    assert text is None


def test_diarization_file_adds_speakers_and_renders(tmp_path: Path) -> None:
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ],
                "embeddings": {},
                "num_speakers": 2,
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "привет"},
        {"start": 6.0, "end": 9.0, "text": "здравствуй"},
    ]
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    # Technical tags in the data; "Голос N" only in the rendered text.
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert text == "Голос 1: привет\n\nГолос 2: здравствуй"


def test_single_speaker_renders_flat(tmp_path: Path) -> None:
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {"segments": [{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"}], "num_speakers": 1}
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "первая"},
        {"start": 4.0, "end": 9.0, "text": "вторая"},
    ]
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_00"]
    assert text == "первая вторая"


def test_corrupt_diarization_file_degrades_to_no_speakers(tmp_path: Path) -> None:
    # A broken artifact must not fail the whole task — the transcript is the
    # valuable output; speaker labels are an enhancement.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text("{not json", encoding="utf-8")
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result == entries
    assert text is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_transcript.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_diarization'`

- [ ] **Step 3: Implement `apply_diarization`**

In `vts/pipeline/steps/transcription.py`, add the import near the top (after the existing `from vts.db.repo import Repo`):

```python
from vts.services.diarization.merge import merge_entries, render_transcript
```

Add this module-level function next to the other ASR domain helpers (after `effective_language`):

```python
def apply_diarization(
    entries: list[dict[str, Any]],
    raw_json_by_index: dict[int, dict[str, Any]],
    diarization_path: Path,
    *,
    min_words: int,
    min_seconds: float,
    min_share: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Attribute entries to speakers, returning the rendered text when diarized.

    Returns the entries unchanged and `None` when there is no diarization
    artifact or it cannot be read: the transcript is the valuable output, and a
    broken speaker artifact must never fail a task.
    """
    if not diarization_path.exists():
        return entries, None
    try:
        payload = json.loads(diarization_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return entries, None
    if not isinstance(payload, dict):
        return entries, None

    diar_segments = payload.get("segments")
    if not isinstance(diar_segments, list) or not diar_segments:
        return entries, None

    merged = merge_entries(entries, raw_json_by_index, diar_segments, min_words, min_seconds)
    return merged, render_transcript(merged, min_share)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diarization_transcript.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Call it from MergeTranscriptStep**

In `vts/pipeline/steps/transcription.py`, `MergeTranscriptStep.run` currently builds `entries` then writes the artifacts. Replace the body from `merged_text = " ".join(merged_tokens).strip()` through the `write_json(...)` call with:

**The index map must be built against the SAME filtered sequence that produces `entries`.** The existing loop appends an entry only when `segment.text.strip()` is non-empty:

```python
            for segment in segments:
                text = segment.text.strip()
                if text:                      # <-- entries skip empty-text chunks
                    entries.append(...)
```

So `enumerate(segments)` over the unfiltered list desynchronises the two the moment any chunk yields empty text — every later entry would then be attributed using **another chunk's words**, scattering speakers at random. Build the map inside that same loop instead, keyed by the entry's own position:

```python
            entries: list[dict[str, Any]] = []
            merged_tokens: list[str] = []
            raw_json_by_index: dict[int, dict[str, Any]] = {}
            for segment in segments:
                text = segment.text.strip()
                if text:
                    merged_tokens.append(text)
                    if isinstance(segment.raw_json, dict) and segment.raw_json:
                        raw_json_by_index[len(entries)] = segment.raw_json
                    entries.append({"start": segment.start_sec, "end": segment.end_sec, "text": text})
```

Then:

```python
            merged_text = " ".join(merged_tokens).strip()
            cleaned_text, cleanup_meta = trim_repetitive_edges(merged_text)

            entries, diarized_text = apply_diarization(
                entries,
                raw_json_by_index,
                st.dirs["outputs"] / "diarization.json",
                min_words=int(getattr(ctx.settings, "diarization_min_words", 2)),
                min_seconds=float(getattr(ctx.settings, "diarization_min_seconds", 0.8)),
                min_share=float(getattr(ctx.settings, "diarization_min_speaker_share", 0.05)),
            )
            final_text = diarized_text if diarized_text is not None else cleaned_text

            write_json(
                transcript_json,
                {
                    "text": final_text,
                    "raw_text": merged_text,
                    "entries": entries,
                    "cleanup": cleanup_meta,
                },
            )
            transcript_txt.write_text(final_text, encoding="utf-8")
```

**Add a regression test for the index alignment** — this is the bug the code above exists to prevent, so pin it:

```python
def test_empty_text_chunk_does_not_shift_word_attribution(tmp_path: Path) -> None:
    # A chunk with empty text produces no entry. Building the word map over the
    # unfiltered chunk list would shift every later entry onto another chunk's
    # words and scatter speakers at random.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # Entry 0 comes from chunk 1 (chunk 0 was silent), so its words are chunk 1's.
    entries = [{"start": 6.0, "end": 9.0, "text": "привет мир"}]
    raw_by_index = {
        0: {
            "segments": [
                {
                    "words": [
                        {"word": "привет", "start": 6.0, "end": 7.0},
                        {"word": "мир", "start": 7.0, "end": 9.0},
                    ]
                }
            ]
        }
    }
    result, _ = apply_diarization(
        entries, raw_by_index, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result[0]["speaker"] == "SPEAKER_01"
```

Then verify the alignment end-to-end against the real loop: confirm by reading `MergeTranscriptStep.run` that `raw_json_by_index` is keyed by `len(entries)` at append time, never by the position in `segments`.

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -q`
Expected: PASS, no regressions — especially `tests/test_pipeline_steps.py` and `tests/test_pipeline_resume.py`

- [ ] **Step 7: Commit**

```bash
git add vts/pipeline/steps/transcription.py tests/test_diarization_transcript.py
git commit -m "feat(diarization): merge speakers into the transcript artifacts (vts-5xz)"
```

---

## Task 7: Chunk on utterance boundaries

**Files:**
- Modify: `vts/services/summarizer.py:401` (`chunk_text`)
- Test: `tests/test_diarization_chunking.py`

**Interfaces:**
- Consumes: nothing
- Produces: `chunk_text(..., split_on_utterances: bool = False)` — when true, windows never cut an utterance

**Why:** `chunk_text` cuts on raw token counts. With speaker labels a mid-utterance cut strands an unlabeled fragment at the head of the next window, and the LLM attributes it to the previous speaker. That error then flows into redacted and the memo.

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_chunking.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_chunking.py -v`
Expected: FAIL — `ImportError: cannot import name 'split_utterances'`

- [ ] **Step 3: Implement `split_utterances`**

In `vts/services/summarizer.py`, add near the top (after the imports):

```python
import re

# Utterances rendered by the diarization merge start with "Голос N: " at the
# beginning of a line. Windows must not cut between a label and its text.
_UTTERANCE_RE = re.compile(r"^Голос \d+: ", re.MULTILINE)


def split_utterances(text: str) -> list[str]:
    """Split rendered dialogue into whole utterances.

    Returns the whole text as one item when it carries no speaker labels, so
    undiarized transcripts flow through unchanged.
    """
    starts = [match.start() for match in _UTTERANCE_RE.finditer(text)]
    if not starts:
        return [text]
    bounds = starts + [len(text)]
    return [text[bounds[i] : bounds[i + 1]].strip() for i in range(len(starts))]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diarization_chunking.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for utterance-aware chunking**

Append to `tests/test_diarization_chunking.py`:

```python
class _FakeTokenizer:
    """Token == word, which keeps the window arithmetic readable in tests."""

    async def tokenize(self, *, model: str, text: str, tokenizer_path: str | None = None) -> list[int]:
        return list(range(len(text.split())))

    async def detokenize(self, *, model: str, tokens: list[int], tokenizer_path: str | None = None) -> str:
        return " ".join(str(t) for t in tokens)


async def test_chunk_text_utterance_mode_never_splits_an_utterance() -> None:
    from vts.services.summarizer import Summarizer

    summarizer = Summarizer.__new__(Summarizer)
    summarizer.tokenize = _FakeTokenizer().tokenize
    summarizer.detokenize = _FakeTokenizer().detokenize

    text = "\n\n".join(f"Голос {i}: слово слово слово слово" for i in (1, 2, 1, 2))
    chunks = await summarizer.chunk_text(
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
    from vts.services.summarizer import Summarizer

    summarizer = Summarizer.__new__(Summarizer)
    summarizer.tokenize = _FakeTokenizer().tokenize
    summarizer.detokenize = _FakeTokenizer().detokenize

    long_utterance = "Голос 1: " + " ".join(["слово"] * 40)
    chunks = await summarizer.chunk_text(
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
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_diarization_chunking.py -v`
Expected: FAIL — `chunk_text() got an unexpected keyword argument 'split_on_utterances'`

- [ ] **Step 7: Implement utterance-aware chunking**

In `vts/services/summarizer.py`, replace the `chunk_text` signature and body (:401):

```python
    async def chunk_text(
        self,
        *,
        text: str,
        model: str,
        window_tokens: int = 2000,
        overlap_ratio: float = 0.15,
        tokenizer_path: str | None = None,
        split_on_utterances: bool = False,
    ) -> list[str]:
        if not text.strip():
            return []
        if split_on_utterances:
            return await self._chunk_by_utterances(
                text=text,
                model=model,
                window_tokens=window_tokens,
                tokenizer_path=tokenizer_path,
            )
        tokens = await self.tokenize(model=model, text=text, tokenizer_path=tokenizer_path)
        if not tokens:
            return []

        overlap = max(int(window_tokens * overlap_ratio), 1)
        step = max(window_tokens - overlap, 1)
        chunks: list[str] = []
        cursor = 0
        while cursor < len(tokens):
            part = tokens[cursor : cursor + window_tokens]
            chunk = await self.detokenize(model=model, tokens=part, tokenizer_path=tokenizer_path)
            if chunk.strip():
                chunks.append(chunk)
            if cursor + window_tokens >= len(tokens):
                break
            cursor += step
        return chunks

    async def _chunk_by_utterances(
        self,
        *,
        text: str,
        model: str,
        window_tokens: int,
        tokenizer_path: str | None,
    ) -> list[str]:
        """Pack whole utterances into windows.

        No overlap: overlap exists to stitch context torn mid-sentence, and
        utterance boundaries tear nothing. Keeping 15% would duplicate whole
        utterances across windows and double them in the summary.
        """
        chunks: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for utterance in split_utterances(text):
            tokens = await self.tokenize(model=model, text=utterance, tokenizer_path=tokenizer_path)
            size = len(tokens)

            if size > window_tokens:
                if current:
                    chunks.append("\n\n".join(current))
                    current, current_tokens = [], 0
                chunks.extend(
                    await self._split_long_utterance(
                        utterance=utterance,
                        tokens=tokens,
                        model=model,
                        window_tokens=window_tokens,
                        tokenizer_path=tokenizer_path,
                    )
                )
                continue

            if current_tokens + size > window_tokens and current:
                chunks.append("\n\n".join(current))
                current, current_tokens = [], 0
            current.append(utterance)
            current_tokens += size

        if current:
            chunks.append("\n\n".join(current))
        return chunks

    async def _split_long_utterance(
        self,
        *,
        utterance: str,
        tokens: list[int],
        model: str,
        window_tokens: int,
        tokenizer_path: str | None,
    ) -> list[str]:
        """Cut an over-long utterance by tokens, repeating its label.

        A ten-minute monologue inside a meeting cannot fit a window, so the
        budget wins — but every continuation carries the label, or the tail
        would be attributed to whoever spoke before.
        """
        match = _UTTERANCE_RE.match(utterance)
        label = match.group(0) if match else ""
        parts: list[str] = []
        cursor = 0
        while cursor < len(tokens):
            part = tokens[cursor : cursor + window_tokens]
            body = await self.detokenize(model=model, tokens=part, tokenizer_path=tokenizer_path)
            body = body.strip()
            if not body:
                break
            parts.append(body if body.startswith(label.strip()) and label else f"{label}{body}")
            cursor += window_tokens
        return parts
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_diarization_chunking.py -v`
Expected: PASS (5 tests)

- [ ] **Step 9: Run the segmentation regression tests**

Run: `pytest tests/test_segmentation_mode.py tests/ -q`
Expected: PASS — `split_on_utterances` defaults to False, so existing behaviour is untouched

- [ ] **Step 10: Commit**

```bash
git add vts/services/summarizer.py tests/test_diarization_chunking.py
git commit -m "feat(diarization): chunk summary windows on utterance boundaries (vts-5xz)"
```

---

## Task 8: Keep labels alive through the rewrite

**Files:**
- Modify: `vts/pipeline/steps/summarization.py:51` (the rewrite prompt), `PrepareSummaryChunksStep.run` (:319-365)
- Test: `tests/test_diarization_prompt.py`

**Interfaces:**
- Consumes: `split_utterances` (Task 7)
- Produces: `rewrite_prompt(base_prompt: str, diarized: bool) -> str`

**Why:** transport is free — `PrepareSummaryChunksStep` already reads `transcript.json → text`, the same field carrying `Голос N:`. Survival is not: the prompt says "rewrite as clean fluent text" and never asks to keep labels, so the LLM may dissolve them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_prompt.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name 'rewrite_prompt'`

- [ ] **Step 3: Implement `rewrite_prompt`**

In `vts/pipeline/steps/summarization.py`, next to the existing prompt constant (:51):

```python
# The rewrite prompt tells the model to clean up speech, which invites it to
# dissolve "Голос 2:" into prose. Diarized tasks get an explicit instruction to
# keep the labels; undiarized ones must not carry this noise.
#
# The unlabelled-block clause is load-bearing, not hedging. The renderer emits
# a bare block for audio diarization never covered, and split_utterances merges
# a mid-transcript one into the PRECEDING utterance — so the model really does
# receive unlabelled text sitting under someone else's label. Saying "each
# utterance starts with a label" would be a lie the model acts on: it would
# attribute that text to the speaker above it, manufacturing the false claim
# the renderer deliberately refused to make.
_KEEP_SPEAKERS_INSTRUCTION = (
    " The text is a dialogue where an utterance may start with a speaker label"
    ' ("Голос 1:", "Голос 2:", ...). Keep every label exactly as it appears, at'
    " the start of that speaker's utterance. Never merge utterances from"
    " different speakers and never invent labels."
    " Some text carries no label — that means the speaker is unknown. Leave it"
    " unlabelled: never attribute it to a nearby speaker and never guess who"
    " said it."
)


def rewrite_prompt(base_prompt: str, diarized: bool) -> str:
    """The window-rewrite prompt, with label preservation for diarized tasks."""
    if not diarized:
        return base_prompt
    return base_prompt + _KEEP_SPEAKERS_INSTRUCTION
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diarization_prompt.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Use it in PrepareSummaryChunksStep**

In `vts/pipeline/steps/summarization.py`, `PrepareSummaryChunksStep.run` reads the transcript at :319-322. After loading `transcript`, detect diarization and pass the flag into chunking:

```python
        transcript_json = st.dirs["outputs"] / "transcript.json"
        if not transcript_json.exists():
            raise RuntimeError("Missing transcript for summarization")
        transcript = json.loads(transcript_json.read_text(encoding="utf-8")).get("text", "")
        if not isinstance(transcript, str) or not transcript.strip():
            st.logger.info("summary chunks skipped: empty transcript")
            return True

        # Labels in the text are the signal — not the task option — because a
        # diarized task with one speaker renders flat text and needs no special
        # handling downstream.
        diarized = len(split_utterances(transcript)) > 1
```

Add the import at the top of the file:

```python
from vts.services.summarizer import split_utterances
```

Then pass `split_on_utterances=diarized` to every `ctx.llm.chunk_text(...)` call in this file, and record the flag in the chunks payload so `SummarizeWindowsStep` can pick it up:

```python
        split_payload = {"chunks": chunks, "segmentation": "split", "diarized": diarized}
```

In `SummarizeWindowsStep`, read the flag back from `summary_chunks.json` and apply it to the segment prompt:

```python
        chunks_payload = json.loads((st.dirs["outputs"] / "summary_chunks.json").read_text(encoding="utf-8"))
        diarized = bool(chunks_payload.get("diarized", False))
        segment_prompt = rewrite_prompt(segment_prompt, diarized)
```

**Note:** locate every `chunk_text` call and every place `segment_prompt` is built before editing — grep first: `grep -n "chunk_text\|segment_prompt" vts/pipeline/steps/summarization.py`. The exact line numbers shift as you edit.

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -q`
Expected: PASS, no regressions — especially `tests/test_segmentation_mode.py` and `tests/test_finalize_loop.py`

- [ ] **Step 7: Commit**

```bash
git add vts/pipeline/steps/summarization.py tests/test_diarization_prompt.py
git commit -m "feat(diarization): keep speaker labels through the window rewrite (vts-5xz)"
```

---

## Task 9: API option

**Files:**
- Modify: `vts/api/schemas.py` — `PresetOptions` (:60-63), `TaskCreateRequest` (:101-114), `UploadInitRequest` (:301-308)
- Test: `tests/test_diarization_api.py`

**Interfaces:**
- Consumes: nothing
- Produces: `diarize: bool = False` on `PresetOptions`, `TaskCreateRequest`, `UploadInitRequest`

- [ ] **Step 1: Write the failing test**

Create `tests/test_diarization_api.py`:

```python
import pytest
from pydantic import ValidationError

from vts.api.schemas import PresetOptions, TaskCreateRequest, UploadInitRequest


def test_diarize_defaults_to_false() -> None:
    assert PresetOptions().diarize is False
    assert TaskCreateRequest(url="https://example.com/v").diarize is False
    assert UploadInitRequest(filename="a.mp4", total_size=1).diarize is False


def test_diarize_accepted() -> None:
    assert TaskCreateRequest(url="https://example.com/v", diarize=True).diarize is True


def test_diarize_requires_transcript() -> None:
    # There is nothing to attribute speakers to without a transcript.
    with pytest.raises(ValidationError, match="diarize requires transcript"):
        TaskCreateRequest(
            url="https://example.com/v",
            diarize=True,
            transcript=False,
            prompts=[],
        )
```

Note `prompts=[]` in the last test: `TaskCreateRequest` defaults `prompts` to a
non-empty list, and the existing `prompts require transcript` rule would fire
first and mask what this test is checking.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diarization_api.py -v`
Expected: FAIL — `TypeError`/`ValidationError`: no `diarize` field

- [ ] **Step 3: Add the field to all three schemas**

In `vts/api/schemas.py`, beside each `transcript: bool` declaration — `PresetOptions` (:63), `TaskCreateRequest` (:105), `UploadInitRequest` (:306) — add:

```python
    diarize: bool = False
```

- [ ] **Step 4: Extend the existing validator**

In `TaskCreateRequest.validate_stage_dependencies` (:108-113), add the new rule beside the existing one:

```python
    @model_validator(mode="after")
    def validate_stage_dependencies(self) -> "TaskCreateRequest":
        if self.prompts and not self.transcript:
            raise ValueError("prompts require transcript")
        if self.diarize and not self.transcript:
            raise ValueError("diarize requires transcript")
        return self
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_diarization_api.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the full test suite**

Run: `pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add vts/api/schemas.py tests/test_diarization_api.py
git commit -m "feat(diarization): diarize option on the submit API (vts-5xz)"
```

---

## Task 10: Diarization container — DONE (2026-07-16)

Built, smoke-tested, and verified offline. Commit `09bbf5f`.

**Files created:** `docker/diarization/{Dockerfile,server.py,requirements.txt,fetch_models.py}`
**Files modified:** `docker-compose.yml` (profile `diarize`), `README.md` (Stack + CC-BY-4.0 attribution)

### What the plan got wrong — read this before touching the container

The plan was written from the pyannote docs. Running it corrected three things:

1. **The model repo is not two `.bin` files.** It also ships `config.yaml`
   (wires the pipeline with `$model`-relative paths) and `plda/` (two `.npz`
   with the VBx clustering parameters). Without them `from_pretrained` has
   nothing to load. All five artifacts are pinned in `fetch_models.py`.

2. **pyannote 4.x returns `DiarizeOutput`, not `Annotation`.** It has three
   fields — `speaker_diarization`, `exclusive_speaker_diarization`,
   `speaker_embeddings` — and carries embeddings **by default**. There is no
   `return_embeddings` flag; passing one is not the API.

3. **Use `exclusive_speaker_diarization`.** The consumer attributes each word to
   exactly one speaker, so overlapping turns would force an arbitrary pick
   downstream. The exclusive variant makes that choice in the model, where the
   acoustic evidence still exists.

### Verified facts (2026-07-16)

- Weights download **anonymously, no token, HTTP 200** — the un-gated claim
  holds. Sizes match the research exactly (5,906,507 and 26,646,242 bytes) and
  the sha256 match what the research found a month earlier, so the weights were
  not swapped.
- HF API reports `gated: False` machine-readably.
- `config.yaml` confirms community-1 uses **VBxClustering**, not spectral —
  independent support for the spec's stack decision.
- Embeddings are `shape=(N, 256)`, positional, aligned with
  `speaker_diarization.labels()`. This is what vts-80i needs.
- **Offline verified:** a full `/diarize` succeeds with `--network none` and
  `huggingface.co` confirmed unreachable (`gaierror`). Nothing is fetched at
  runtime.

### Environment notes

- The build context must be an absolute path if your shell's cwd may have moved.
- Rootless podman here does not publish ports to the host; drive the container
  with `docker exec ... python -c` against `http://localhost:9100` from inside.
- Synthetic tones diarize to one speaker. That is not a failure — pyannote is
  trained on human speech. Use them to check the wire contract only; real
  speaker counts need a real recording (Task 11).


## Task 11: End-to-end verification on a real meeting

The thresholds in this plan are estimates. This task is where they meet reality.

**Files:** none — this is calibration, not code.

- [ ] **Step 1: Run a real 4+ person meeting through the pipeline**

Submit a real recording with `diarize: true`, `transcript: true`, and a summary prompt.

- [ ] **Step 2: Check the artifacts**

```bash
cat outputs/diarization.json | python -c "import json,sys; d=json.load(sys.stdin); print(d['num_speakers'], len(d['segments']), list(d['embeddings']))"
head -40 outputs/transcript.txt
head -40 outputs/redacted_transcript.txt
```

Verify, in order:
- `num_speakers` matches the real number of participants
- `transcript.txt` carries `Голос N:` and reads as a dialogue, not shredded lapsha
- **`redacted_transcript.txt` still carries the labels** — this is the load-bearing check. If the LLM dissolved them despite Task 8's instruction, the chain to the memo is broken, and the spec's open question ("насколько надёжно LLM сохраняет метки") is answered No. Report this rather than working around it.
- the summary/memo attributes ideas and tasks to specific voices

- [ ] **Step 3: Add diarize's seed step weight — now that you can measure it**

`SEED_STEP_WEIGHTS` (`vts/metrics/step_weights.py:81`) holds measured seconds per
step and drives the progress bar. `diarize` is not in it, so `merge_with_seed`
(`:56-69`, which iterates `seed.items()` and emits only seed keys) silently drops
every real per-user `diarize` sample — the step contributes nothing to progress
forever.

This was deliberately deferred to here: a seed value is a *measurement*, and until
this task no one had ever run the container on real audio. Inventing a number
earlier would have written a fiction into the progress bar.

Note this is not breakage — `pack_window_notes` already sits in `DAG_HEAD` without
a seed weight, so the precedent exists and the pipeline tolerates it. It is a
progress-accuracy gap.

From the run in Step 1, take the actual `diarize` duration (the step logs it; also
check the task's step rows) and add it:

```python
    "transcribe_segments": 174.8,
    "diarize": <measured seconds>,
    "merge_transcript": 0.1,
```

`tests/test_step_weights.py:108` asserts `len(SEED_STEP_WEIGHTS) == 10` — a
deliberate tripwire. Bump it to 11 and say why in the commit.

Beware: diarization time scales with audio length, and the seed is a single
number. Use a duration typical of your meetings rather than a short fixture, and
sanity-check it against `transcribe_segments`' 174.8 — if diarization lands wildly
above that, the progress bar will feel wrong and the number deserves a second run.

- [ ] **Step 4: Calibrate the thresholds**

If backchannels ("угу") shred the text → raise `diarization_min_words` / `diarization_min_seconds`.
If real short replies vanish → lower them.
If a phantom speaker survives → raise `diarization_min_speaker_share`.

Record what you changed and why in the bd issue.

- [ ] **Step 5: Update the spec's open questions**

Edit `docs/superpowers/specs/2026-07-15-speaker-diarization-design.md`, replacing the estimated thresholds in the "Открытые вопросы" section with the calibrated values and the evidence.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-07-15-speaker-diarization-design.md config.yaml vts/metrics/step_weights.py tests/test_step_weights.py
git commit -m "chore(diarization): calibrate thresholds and seed weight on a real meeting (vts-5xz)"
```

---

## Self-Review Notes

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Контейнер pyannote, веса по sha256, без рантайм-загрузки | 10 |
| DiarizationBackend + PyannoteBackend, httpx | 3 |
| PyTorch не в requirements.txt | Global Constraints + 10 |
| Шаг diarize после транскрипции, мёрж по перекрытию | 5, 6 |
| Эмбеддинги в diarization.json для vts-80i | 5 |
| Рендер монолог/диалог, порог доминирования | 2 |
| submit_video + pipeline options принимают diarize | 9 |
| Конфиг diarization_backend + url | 4 |
| Тесты: мок бэкенда, мерж-логика, авто-переключение | 1, 2, 3, 5, 6 |
| Verified offline | 10 (Step 8) |
| Атрибуция CC-BY-4.0 | 10 (Step 9) |
| Спикеры доезжают до redacted и саммари | 8, 11 |
| Нарезка окон по границам реплик, overlap off | 7 |
| Калибровка порогов на реальной встрече | 11 |

**Deliberate deviations from the spec:**
- Spec says `speaker_label` merge lives in `MergeTranscriptStep`; the plan puts the pure logic in `vts/services/diarization/merge.py` and calls it from the step. The step already carries DB access, artifact writing, and cleanup — adding merge rules there would make it untestable without a database.
- `usable_words` rejects subword fragments, which the spec does not mention. Discovered while reading `tests/test_transcription_backends.py`: whisper.cpp returns subword tokens in `words`, so the presence of that key does not imply usable words. Without this check the `cpp` fallback would cut words in half instead of degrading cleanly.

**Fixed during self-review:**
- Task 9 named a non-existent `SubmitOptions` class. The real schemas are `PresetOptions` (:60), `TaskCreateRequest` (:101, which owns the `prompts require transcript` validator) and `UploadInitRequest` (:301) — all three verified in the source.
- Removed 7 `@pytest.mark.asyncio` decorators: `pytest.ini` sets `asyncio_mode = auto`.

**Known risk carried into execution:**
- ~~Task 6 Step 5 assumes entry index == chunk index.~~ **RESOLVED 2026-07-15 — it was a real bug, now fixed in the plan.** The Task 1 reviewer traced it and the controller confirmed it in source: `MergeTranscriptStep.run` appends an entry only for non-empty text, so `enumerate(segments)` over the unfiltered list desynchronises on the first silent chunk and attributes every later entry using another chunk's words. Task 6 now builds the map keyed by `len(entries)` at append time and carries a regression test pinning it.
- Task 10's sha256 values are placeholders by necessity — they must be verified against the live mirror at build time (Step 3), which doubles as re-verification of the un-gated claim.
- Task 10's `server.py` calls `Pipeline.from_pretrained` on a local dir and `return_embeddings=True`. Both are read from pyannote 4.x docs, not run — the container smoke test (Step 7) is what proves the shape. If the embeddings API differs, fix `server.py` and keep the wire contract (`{"segments", "embeddings", "num_speakers"}`) intact; the client and every test depend on it, not on pyannote's internals.
