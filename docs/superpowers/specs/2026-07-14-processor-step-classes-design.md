# Рефакторинг processor.py: Step-классы + PipelineContext (vts-08d)

**Дата:** 2026-07-14
**bd:** vts-08d
**Тип:** рефакторинг без изменения поведения (behavior-preserving)

## Цель

`vts/pipeline/processor.py` разросся до ~2494 строк: один `TaskProcessor` держит
12 `step_*`-методов (~40-300 строк каждый) и ~40 приватных хелперов, которые
шаги делят между собой. Файл слишком большой, шаги не изолированы, тестировать
шаг по-одному тяжело.

Вынести каждый шаг в отдельный класс `Step` (базовый ABC + наследники);
`_run_step` диспетчеризует по реестру вместо `getattr(self, f"step_{name}")`.
Общие зависимости и инфраструктурные хелперы переезжают в явный
`PipelineContext`. **Поведение пайплайна не меняется** — это чистая
реструктуризация.

## Принятые решения

- **Контекст шагов — явный `PipelineContext`-объект** (не ссылка на processor,
  не god-object). Шаги получают его в `run(ctx, state)`.
- **Полный рефакторинг за один проход** (12 шагов + ctx), опираясь на 628
  существующих тестов как сетку безопасности. Не поэтапно.
- **Инфраструктурные хелперы → `PipelineContext`; доменные → рядом с Step**
  (чистые преобразования — функции модуля; трогающие сервис/БД — через ctx).
- **Раскладка по доменам** в `vts/pipeline/steps/` (media / transcription /
  summarization), а не файл-на-шаг и не один общий файл.
- **`dry_run` → метод `already_done()`**; **`lane_for_step()` → атрибут
  `Step.lane`**.
- **5 «белоящичных» тестов переписываются на новую Step-поверхность в этом же
  PR** (тестируют изолированный Step через ctx-стабы, проверяя то же поведение).

## Архитектура

### PipelineContext (`vts/pipeline/context.py`)

Явный контейнер общих зависимостей и инфраструктурных хелперов, сейчас висящих
на `TaskProcessor`.

- **Сервисы:** `llm`, `whisper`, `bus`, `lanes`, `settings`, `session_factory`,
  метрики (`get_emitter`).
- **Инфра-хелперы** (общие для всех шагов, переносятся с `TaskProcessor`
  как есть по логике): `gpu_slot()`, `refresh_task()`, `mark_waiting()`,
  `mark_running()`, `get_n_ctx()`, `persist_summary_progress()`,
  `persist_detected_language()`, `save_task_source_title()`,
  `get_user_preferred_ytdlp_client()`, `set_user_preferred_ytdlp_client()`,
  `check_paused()`, `send_push_safe()`, `task_url()`.

`PipelineContext` — не новый god-object: он держит ТОЛЬКО кросс-доменную
инфраструктуру. Доменная логика (ASR, summary) живёт с шагами.

### Step ABC (`vts/pipeline/steps/base.py`)

```python
@dataclass
class StepState:
    task_id: uuid.UUID
    user_id: str
    dirs: dict[str, Path]
    logger: logging.Logger
    task_options: dict[str, Any]


class Step(ABC):
    name: str                    # registry name, e.g. "download"
    lane: str | None = None      # replaces lane_for_step()

    @abstractmethod
    async def run(self, ctx: PipelineContext, st: StepState) -> None: ...

    async def already_done(self, ctx: PipelineContext, st: StepState) -> bool:
        """Return True if the step's output already exists (resume). Replaces
        the old dry_run=True path. Default: never short-circuits."""
        return False
```

`StepState` replaces the current five positional args
(`task_id, user_id, dirs, logger, task_options`).

### Реестр и диспетчеризация (`vts/pipeline/steps/registry.py`)

- `STEP_REGISTRY: dict[str, Step]` — статичные шаги (download … summarize_windows,
  pack_window_notes) регистрируются по имени.
- **Finalize — особый случай** (несёт `source`/`id`): `FinalizePromptStep`
  принимает их в `__init__`. `resolve_step(step_name)` создаёт правильный
  инстанс:
  - `"summarize_final"` → `FinalizePromptStep(source="system", id="summary")`;
  - `"finalize:<ref>"` → `FinalizePromptStep(source=..., id=...)` через
    `parse_ref`;
  - иначе → `STEP_REGISTRY[step_name]`.

`_run_step` заменяет `getattr`-резолв + `functools.partial` на
`step = resolve_step(step_name)` → `step.run(ctx, st)`, а dry-run-проверку — на
`if await step.already_done(ctx, st): return`. Логика вокруг (skip-if-disabled,
lane-acquire, статусы/события шага) сохраняется; `lane = step.lane` вместо
`lane_for_step(step_name)`.

## Раскладка по файлам (`vts/pipeline/steps/`)

| Файл | Step-классы | Доменные хелперы рядом |
|---|---|---|
| `base.py` | `Step` ABC, `StepState` | — |
| `registry.py` | `STEP_REGISTRY`, `resolve_step()` | finalize-фабрика |
| `media.py` | `DownloadStep`, `ExtractAudioStep`, `TrimInitialSilenceStep`, `SegmentAudioStep` | — |
| `transcription.py` | `DetectLanguageStep`, `TranscribeSegmentsStep`, `MergeTranscriptStep` | `is_probable_asr_hallucination`, `transcript_quality_score`, `trim_repetitive_edges`, `normalize_token`, `tail_prompt`, `transcribe_audio_path`, `effective_language`, `normalize_language` |
| `summarization.py` | `PrepareLlamaModelStep`, `PrepareSummaryChunksStep`, `SummarizeWindowsStep`, `PackWindowNotesStep`, `FinalizePromptStep` | `token_budget_config`, `render_prompt_budget_vars`, `render_prompt_with_language`, `language_display_name`, `extract_window_text`, `log_metrics`, `resolve_prompt_text`, `prompt_display_name`, `persist_prompt_result` |

Доменный хелпер — **функция модуля**, если это чистое преобразование
(`is_probable_asr_hallucination`, `token_budget_config`, `language_display_name`,
…). Если он трогает сервис/БД (`resolve_prompt_text`, `persist_prompt_result`)
— метод соответствующего Step или принимает `ctx`. Классификация уточняется при
реализации по этому признаку.

**Хелперы, общие для нескольких доменов:** `effective_language` и
`normalize_language` нужны и транскрипции, и подготовке промптов (summary). Они
чистые функции — размещаются в `transcription.py` и импортируются
`summarization.py` оттуда (без дублирования); если связь окажется неудобной,
выносятся в общий `vts/pipeline/steps/_shared.py`. Решение — при реализации.

## `TaskProcessor` после рефакторинга (`processor.py`, ~400-500 строк)

Тонкий оркестратор. Остаётся:
- `process_task` (donor-clone, цикл шагов, статусы задачи, метрики, `_TaskGone`
  quiet-exit — vts-d64);
- `_run_step` (диспетчер через реестр);
- `_clone_from_donor`, `_task_options`, `_is_step_enabled`, `_task_logger`,
  `_send_push_safe`, `_refresh_task`, `_check_paused`, `_mark_*`, `_gpu_slot`;
- создание `PipelineContext` из своих сервисов.

Часть инфра-методов (`_gpu_slot`, `_refresh_task`, `_mark_*`, `_check_paused`,
`_send_push_safe`, `_get_n_ctx`, `_persist_*`, `_get/set_user_preferred_*`,
`_task_url`, `_get_emitter`) переезжает в `PipelineContext`; `process_task`
вызывает их через `ctx` или напрямую делегирует. Точное распределение (что
остаётся на processor как оркестрация vs. что уходит в ctx как инфра для шагов)
— в плане.

## Публичная поверхность — НЕ меняется

- `from vts.pipeline.processor import TaskProcessor` (воркер + тесты) — остаётся.
- `TaskProcessor.__init__(session_factory, redis, settings, lanes=...)` и
  `process_task(task_id)` — сигнатуры без изменений.
- `TaskPaused` — остаётся в `processor.py`.
- Поведение шагов, порядок DAG, статусы, SSE-события, resume-семантика,
  ночной режим, lane-приоритеты — без изменений.

## Удаляется

- `step_*`-методы и `getattr`-диспетчер с `TaskProcessor`.
- `lane_for_step()` и `STEP_LANES` из `vts/pipeline/types.py` (маппинг переезжает
  в атрибут `Step.lane`). `build_dag_steps`/`DAG_HEAD` остаются.
- Доменные хелперы с `TaskProcessor` (переезжают в steps-модули).

## Тестирование

- **5 «белоящичных» тестов** (`test_pipeline_resume`, `test_finalize_loop`,
  `test_segmentation_mode`, `test_processor_lanes`, `test_task_title_preservation`)
  переписываются на новую поверхность: строят `PipelineContext` со стабами и
  вызывают `StepClass().run(ctx, state)` / `.already_done(...)`. Каждый
  проверяет то же поведение, что и раньше (не ослабляя).
- **Остальные ~623 теста** идут через API/воркер/`process_task` целиком, не
  трогают step-методы — остаются валидными, ловят регрессии оркестрации.
- После каждой доменной порции — полный `pytest -q` зелёный.
- Финальная проверка: полный сьют + (если доступно окружение LLM/whisper) прогон
  реального короткого видео через `/verify`; иначе полагаемся на сьют и это
  отмечается в отчёте.

## Порядок реализации

1. `PipelineContext` + `Step` ABC + `StepState` + пустой реестр (каркас).
2. Перенос по доменам media → transcription → summarization. После каждого:
   обновить реестр, `_run_step` диспетчеризует переехавшие через реестр +
   `getattr` для непереехавших (гибрид живёт ВНУТРИ PR, между коммитами),
   полный сьют зелёный, соответствующие «белоящичные» тесты переписаны.
3. Удалить старые step-методы, `getattr`-диспетчер, `lane_for_step`/`STEP_LANES`,
   доменные хелперы с `TaskProcessor`.
4. Финальная зачистка `processor.py`; подтвердить, что `TaskProcessor` тонкий
   (~400-500 строк), полный сьют зелёный.

## Вне объёма

- Изменение поведения любого шага.
- Изменение DAG-порядка, статусов, событий, resume-логики.
- Плагинность шагов / внешняя регистрация (реестр внутренний).
- Рефакторинг воркера, API, UI.
