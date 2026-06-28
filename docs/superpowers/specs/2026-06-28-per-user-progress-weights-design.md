# Per-user адаптивные веса прогресса — periodic recompute

**Задача:** vts-8cm (P3). Followup к vts-b6t
([step-weights-recompute-design](2026-06-28-step-weights-recompute-design.md), раздел Followup).

## Проблема

После vts-b6t веса шагов прогресс-бара — глобальные константы в `app.js`,
разово перекалиброванные по всем пользователям. Задачи разных пользователей
имеют разный профиль (длина видео, число окон, язык), поэтому единый вес
неточен. Нужно: веса per-user, хранятся в БД, периодически пересчитываются,
адаптируются под реальную нагрузку конкретного пользователя.

## Решения (зафиксированы при брейнсторминге)

1. **Где считаем:** фоновый цикл в процессе worker (`vts/worker/main.py`).
   Один инстанс воркера — гонок нет; пересчёт вне request-пути.
2. **Порог «мало данных»:** per-step. Шаг обновляется только если у пользователя
   `>= min_samples` completed-длительностей этого шага; иначе остаётся seed.
   Дефолт `min_samples = 5`. Агрегируем по ВСЕМ данным пользователя; интервал —
   это частота пересчёта, не окно данных.
3. **Endpoint:** `GET /api/progress-weights` отдаёт веса текущего пользователя
   (пересчитанные + seed для недостающих шагов). Seed — серверный источник.
   Хардкод-константы в `app.js` остаются как офлайн-фолбэк.
4. **Конвенция окна:** выравниваем на `total - 1` (истинное число окон) и на
   сервере, и в клиенте. `aggregate_step_weights` параметризуется
   `window_offset` (0 = legacy b6t-скрипт, 1 = per-user).

## Архитектура (границы единиц)

Чистая математика — в `vts/metrics/`. SQL/persistence — в `vts/db/repo.py`.
Оркестрация — в `vts/services/step_weights_recompute.py`. Планировщик — в
worker lifespan. Endpoint — в `vts/api/main.py`. Клиент — `app.js`.

### 1. Хранение — миграция `0012`, таблица `user_step_weights`

| колонка | тип | назначение |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → users (ondelete CASCADE) | владелец |
| `weights` | JSON | `{step_name: median_seconds}`; `summarize_windows` — на реальное окно |
| `final_summary_fallback` | Float \| null | медиана `summarize_final` |
| `computed_at` | timestamptz | время последнего пересчёта |
| `sample_counts` | JSON | `{step_name: n}` — сколько сэмплов в каждом весе |

- `UniqueConstraint(user_id, name="uq_user_step_weights_user")` — одна строка на
  пользователя.
- `Index("ix_user_step_weights_user", "user_id")`.
- Сбор длительностей НЕ требует изменения схемы — они выводятся из
  `steps → tasks.user_id` (`started_at`/`finished_at` уже персистятся). Новая
  таблица хранит только результат пересчёта.
- Модель `UserStepWeights` в `vts/db/models.py`. Миграция `0012` chains from
  `0011_presets` (down_revision = "0011").

### 2. Агрегация — расширение `vts/metrics/step_weights.py`

- `aggregate_step_weights(rows, *, window_offset: int = 0) -> dict[str, float]`:
  для `summarize_windows` делитель = `window_total - window_offset`. Дефолт `0`
  → поведение b6t не меняется (его тесты зелёные). Per-user зовёт
  `window_offset=1`. Строки, где `window_total - window_offset < 1`, пропускаются
  для этого шага.
- `step_sample_counts(rows, *, window_offset: int = 0) -> dict[str, int]` —
  возвращает `{step_name: n}`: сколько строк дали валидный сэмпл для каждого шага
  (для `summarize_windows` учитывает тот же `window_offset` guard). Чистая.
- `SEED_STEP_WEIGHTS: dict[str, float]` — серверный источник seed (значения из
  `app.js` после b6t: download 5.5, extract_audio 2.0, trim_initial_silence 0.3,
  segment_audio 1.2, detect_language 2.6, transcribe_segments 174.8,
  merge_transcript 0.1, prepare_llama_model 6.3, prepare_summary_chunks 0.1,
  summarize_windows 598.4).
- `SEED_FINAL_SUMMARY_FALLBACK: float = 514.4`.
- `merge_with_seed(computed, sample_counts, *, min_samples, seed) -> dict[str, float]`:
  для каждого шага из `seed` берёт `computed[step]`, если
  `sample_counts.get(step, 0) >= min_samples`, иначе `seed[step]`. Возвращает
  полный набор (все seed-шаги присутствуют). Чистая, тестируемая.
- `final_summary_fallback(rows, *, min_samples, seed_fallback) -> float` — медиана
  длительностей строк с `name == "summarize_final"`, если их `>= min_samples`,
  иначе `seed_fallback`. Чистая. (Та же логика, что `_final_summary_fallback` в
  b6t-скрипте, но с порогом и seed.)

> Note: `summarize_windows` seed (598.4) уже в b6t-шкале «на шаг» (raw total).
> Per-user computed value будет в шкале «на реальное окно» (`total-1`). Это РАЗНЫЕ
> шкалы. Поскольку клиент после выравнивания делит на `total-1`, seed-значение для
> `summarize_windows` в `SEED_STEP_WEIGHTS` должно быть в шкале «на реальное окно»
> тоже. Берём seed «на окно» = b6t per-window median (74.8 — из вывода b6t-скрипта:
> "per-window median = 74.8 s"). Т.е. `SEED_STEP_WEIGHTS["summarize_windows"] = 74.8`,
> и клиент умножает на число окон. **РЕШЕНИЕ для плана:** привести клиентскую
> формулу и seed к единой «на реальное окно» шкале — endpoint и seed отдают
> «секунд на окно», `estimateFinalSummaryWeight` умножает на `(total-1)`. Зафиксировать
> точную формулу в плане; следить, чтобы офлайн-фолбэк `app.js` остался согласован.

### 3. Сбор + сервис пересчёта

**Repo (`vts/db/repo.py`):**
- `step_durations_for_user(user_id) -> list[StepDuration]` — SQL: `steps` join
  `tasks`, фильтр `tasks.status=completed AND steps.status=completed AND
  tasks.user_id=:uid`. Тянет `steps.name`, `(finished_at - started_at)` сек,
  `tasks.summary_progress->>'total'`. Пропускает null/отрицательные длительности.
- `upsert_user_step_weights(user_id, weights, final_summary_fallback, computed_at, sample_counts)`.
- `get_user_step_weights(user_id) -> UserStepWeights | None`.
- `users_with_completed_tasks() -> list[uuid.UUID]`.

**Сервис (`vts/services/step_weights_recompute.py`):**
- `recompute_for_user(session, user_id, *, min_samples, seed, seed_fallback) -> bool`:
  собирает строки → `aggregate_step_weights(rows, window_offset=1)` +
  `step_sample_counts(rows, window_offset=1)` → `merge_with_seed(...)`. Fallback
  считается отдельной чистой функцией `final_summary_fallback(rows, *, min_samples,
  seed_fallback) -> float` в `vts/metrics/step_weights.py` = медиана длительностей
  строк с `name == "summarize_final"`, если их `>= min_samples`, иначе
  `seed_fallback`. (`aggregate_step_weights` намеренно НЕ агрегирует
  `summarize_final` — finalize-шаги обрабатываются клиентом через
  `estimateFinalSummaryWeight`, не как обычный шаг.) Затем upsert. Возвращает True
  если записал, False если у пользователя ноль completed-задач (тогда на чтении
  вернётся seed).
- `recompute_all_users(session_factory, *, min_samples, seed, seed_fallback) -> int`:
  итерирует `users_with_completed_tasks()`, зовёт `recompute_for_user`, считает
  обновлённых, логирует итог. Каждый пользователь — в своей попытке try/except,
  чтобы один сбой не ронял весь проход.

### 4. Планировщик — worker lifespan

В `vts/worker/main.py` (рядом с `_pump`):
- `_step_weights_loop()`: короткий стартовый джиттер, затем `while True`:
  `recompute_all_users(...)` в try/except (лог при ошибке, цикл не падает) →
  `await asyncio.sleep(interval_seconds)`.
- Запускается при старте воркера (`asyncio.create_task`), если
  `settings.progress_weights_enabled`.

**Конфиг (`vts/core/config.py`, `Settings`):**
- `progress_weights_recompute_interval_seconds: int = 604800` (неделя).
- `progress_weights_min_samples: int = 5`.
- `progress_weights_enabled: bool = True` (рубильник; выключен → все на seed).

### 5. Endpoint + клиент

**Endpoint `GET /api/progress-weights` (`vts/api/main.py`):**
- Для текущего пользователя: `get_user_step_weights(user_id)`. Если строка есть —
  отдаём её `weights` + `final_summary_fallback` (они уже полные, merge сделан при
  пересчёте). Если нет — отдаём чистый seed (`SEED_STEP_WEIGHTS`,
  `SEED_FINAL_SUMMARY_FALLBACK`). Endpoint ВСЕГДА отдаёт полный валидный набор.
- Pydantic `ProgressWeightsOut(weights: dict[str, float], final_summary_fallback: float)`.

**Клиент (`app.js`):**
- В `bootstrap()` рядом с `loadPrompts()`: `await loadProgressWeights()` — зовёт
  `/api/progress-weights`, кладёт в модульные `serverStepWeights` /
  `serverFinalFallback`. При ошибке — оставляет их `null` (фолбэк на хардкод).
- `getStepWeight` читает `serverStepWeights[step]` с фолбэком на
  `STEP_WEIGHT_SECONDS[step]`. `estimateFinalSummaryWeight` использует
  `serverStepWeights.summarize_windows` (на окно) × `(total-1)` логику и
  `serverFinalFallback` с фолбэком на `FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS`.
- Хардкод-константы остаются как офлайн-фолбэк. DOM не добавляется.
- Версия `vts/__init__.py` бампается (клиент-facing).

## Тестирование

- `tests/test_step_weights.py` (расширить): `window_offset=1` нормализация;
  `step_sample_counts`; `merge_with_seed` (ниже порога → seed, на/выше → computed,
  пустой computed → полностью seed); `final_summary_fallback` (ниже порога →
  seed_fallback, на/выше → медиана). b6t-кейсы остаются зелёными (дефолт offset=0).
- `tests/test_step_weights_recompute.py` (Postgres-фикстура): `recompute_for_user`
  пишет строку с правильными весами/счётчиками; пользователь без данных → не пишет;
  per-step порог: шаг с <min_samples остаётся seed.
- `tests/test_progress_weights_api.py`: строка есть → её веса; строки нет → seed;
  изоляция per-user (юзер A не видит веса B).
- UI verifier: стаб `/api/progress-weights`; прогресс-сценарий зелёный; при 500 —
  клиент падает на хардкод-фолбэк (прогресс всё равно считается).

## Изменения в коде (итог)

- **Новое:** `vts/services/step_weights_recompute.py`,
  `alembic/versions/0012_user_step_weights.py`, `tests/test_step_weights_recompute.py`,
  `tests/test_progress_weights_api.py`.
- **Правка:** `vts/metrics/step_weights.py` (+`window_offset`, `step_sample_counts`,
  `merge_with_seed`, `SEED_*`), `vts/db/models.py` (модель `UserStepWeights`),
  `vts/db/repo.py` (4 метода), `vts/worker/main.py` (loop), `vts/core/config.py`
  (3 настройки), `vts/api/main.py` (endpoint), `vts/api/schemas.py`
  (`ProgressWeightsOut`), `vts/static/app.js` (loadProgressWeights + чтение),
  `vts/__init__.py` (версия).
- **Без** изменения схемы для сбора длительностей (выводятся из существующих
  `steps`/`tasks`).

## Out of scope
- UI для ручного просмотра/сброса своих весов.
- Глобальный (кросс-юзер) пересчёт seed-констант — это была b6t (one-off).
- Декей старых данных / окно «последние N дней» (агрегируем по всем данным).
