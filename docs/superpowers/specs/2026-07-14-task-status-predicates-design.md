# Единая семантическая модель статусов задачи (vts-c2n)

**Дата:** 2026-07-14
**bd:** vts-c2n
**Тип:** рефакторинг без изменения поведения (behavior-preserving)

## Цель

Статусная логика размазана по коду сырыми сравнениями строк/enum. Frontend
`vts/static/app.js`: ~50 сравнений `baseStatus === "queued"/"running"/"waiting"/…`
в ~30 местах, без единого источника семантики («активна ли», «можно ли
паузить», «показывать ли прогресс»). Backend: 41 использование `TaskStatus.`
в 9 файлах — enum типизирован, но семантические множества (`can_pause_task`,
`can_resume_task`, archive-гейт, MCP-terminal, process_task skip) разрознены и
уже РАСХОДЯТСЯ.

Отсутствие семантических предикатов породило баг vts-qzl (waiting забыли в 2 из
3 прогресс-функций) и кросс-задачный вопрос waiting+pause из VOS-85.

Ввести именованные семантические предикаты над `TaskStatus` с единственным
источником в Python, доставляемые в JS, и заменить сырые сравнения на них.
**Поведение не меняется** — каждый предикат кодирует ровно текущее множество.

## Обнаруженные факты (обоснование дизайна)

Уже есть ТРИ разных определения «terminal» — доказательство, что «terminal» не
одно понятие, а разные вопросы:
- MCP wait (`vts/mcp/tools.py:547`): `{completed, failed, canceled}`
- process_task skip (`vts/pipeline/processor.py:76`): `{canceled, completed, archived}`
- archive-гейт (`vts/api/main.py:1954`): `not in {completed, failed}`

Прочие множества:
- `can_pause_task` (main.py:92) = `{queued, running, waiting}`
- `can_resume_task` (main.py:96) = `{paused, failed}`
- recovery requeue (repo.py:186) = `{running, waiting}` (SQL `.in_()`)
- waiting↔running переходы (context.py) — не множества, а конкретные переходы.

## Принятые решения

- **Python — единственный источник семантики.** JS получает результаты, не
  дублирует правила.
- **Разные вопросы — разные предикаты.** НЕ вводить один `is_terminal`. Каждое
  текущее множество получает своё имя; расхождения становятся видимыми, но НЕ
  трогаются (behavior-preserving). Найденные баги-расхождения — отдельные
  bd-задачи, вне этого рефакторинга.
- **Доставка в JS — гибрид по признаку «зависит ли от чего-то кроме статуса».**
  Зависит ТОЛЬКО от статуса (pause/resume/archive + чисто-статусные) → статичная
  карта `status → флаги`, отдаётся один раз. Зависит от данных задачи помимо
  статуса (restart-summary — смотрит на шаги/результаты) → per-task
  `capabilities` в TaskOut. Эта граница делает pause/resume/archive устойчивыми
  к SSE (которое меняет статус без нового TaskOut).
- **Только TaskStatus.** StepStatus и сам enum — вне объёма.

## Архитектура

### Python-модуль `vts/services/task_status.py`

Чистые функции над `TaskStatus`, ноль зависимостей кроме enum. Основа —
именованные множества-константы, поверх которых строятся и предикаты, и SQL:

```python
ACTIVE_STATUSES = {TaskStatus.running, TaskStatus.waiting}
PENDING_STATUSES = {TaskStatus.queued, TaskStatus.waiting}
FINISHED_STATUSES = {TaskStatus.completed, TaskStatus.failed,
                     TaskStatus.canceled, TaskStatus.archived}
PAUSABLE_STATUSES = {TaskStatus.queued, TaskStatus.running, TaskStatus.waiting}
RESUMABLE_STATUSES = {TaskStatus.paused, TaskStatus.failed}
ARCHIVABLE_STATUSES = {TaskStatus.completed, TaskStatus.failed}
SKIPPABLE_ON_START_STATUSES = {TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}
TERMINAL_FOR_WAIT_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled}

# Pure-status predicates (depend only on the status value)
def is_active(s): return s in ACTIVE_STATUSES
def is_pending(s): return s in PENDING_STATUSES
def is_finished(s): return s in FINISHED_STATUSES
def shows_progress(s): return is_active(s) or s in {TaskStatus.completed, TaskStatus.failed}
def can_pause(s): return s in PAUSABLE_STATUSES
def can_resume(s): return s in RESUMABLE_STATUSES
def can_archive(s): return s in ARCHIVABLE_STATUSES
def is_skippable_on_start(s): return s in SKIPPABLE_ON_START_STATUSES
def is_terminal_for_wait(s): return s in TERMINAL_FOR_WAIT_STATUSES
```

Каждый предикат кодирует ТОЧНО текущее множество (см. «Обнаруженные факты») —
никакой унификации, никакого изменения поведения.

**Статус-флаги для JS** (одна карта, отдаётся один раз):
```python
def status_flags() -> dict[str, dict[str, bool]]:
    return {
        s.value: {
            "is_active": is_active(s), "is_pending": is_pending(s),
            "is_finished": is_finished(s), "shows_progress": shows_progress(s),
            "can_pause": can_pause(s), "can_resume": can_resume(s),
            "can_archive": can_archive(s),
        }
        for s in TaskStatus
    }
```

`shows_progress`, `is_active`, `is_pending`, `is_finished`, `can_pause`,
`can_resume`, `can_archive` покрывают все чисто-статусные фронт-решения.

### Task-dependent capability: restart-summary

Restart зависит от шагов/результатов, не только от статуса. Существующие
`can_restart_summary_task(task)` (main.py:110) и `can_restart_final_summary_task(task)`
(main.py:122) — источник; они остаются в Python. В сериализацию добавляется
per-task флаг(и):
```python
class TaskCapabilities(BaseModel):
    can_restart_summary: bool
    can_restart_final_summary: bool
```
`serialize_task`/`serialize_task_compact` вычисляют его из этих двух функций.
Pause/resume/archive в capabilities НЕ кладутся (они чисто-статусные → в карте).

### Доставка карты в JS

`status_flags()` отдаётся один раз. Точка доставки — расширить существующий
`/api/uploads/config` в общий bootstrap ИЛИ добавить `/api/config` с
`{status_flags: {...}}`. Решение при реализации: предпочтение — отдельный
маленький `/api/status-config` (или поле в существующем config-эндпоинте), не
инлайн в index.html (чтобы не завязываться на шаблон). app.js грузит карту при
bootstrap до первого рендера задач.

### JS-модуль `vts/static/status-predicates.js`

Читает карту (из bootstrap) и per-task capabilities (из TaskOut), даёт функции:
```js
// pure-status, from the delivered map:
statusPred.isActive(status) / isPending / isFinished / showsProgress
statusPred.canPause(status) / canResume / canArchive
// task-dependent, from runtime.capabilities (TaskOut):
taskCap.canRestartSummary(runtime) / canRestartFinalSummary(runtime)
```
Подключается в `index.html` ПЕРЕД `<script src="app.js">` (app.js без defer —
зависимости обязаны идти раньше; см. project memory script-dom-order).

## Точки замены (behavior-preserving)

**Backend:**
- `vts/api/main.py`: `can_pause_task`/`can_resume_task` делегируют в
  `task_status.can_pause/can_resume` (или их тела заменяются вызовом); archive-
  гейт (1954) → `task_status.can_archive(task.status)`. Сериализаторы (675, 736)
  без изменений по логике позиций; добавляют `capabilities`.
- `vts/pipeline/processor.py:76`: skip-множество → `is_skippable_on_start`.
- `vts/mcp/tools.py:547-562`: `_TERMINAL` → `is_terminal_for_wait` (или
  `TERMINAL_FOR_WAIT_STATUSES`; учесть, что MCP сравнивает `str(task.status)`).
- `vts/db/repo.py:186`: `status.in_([running, waiting])` → использовать
  `ACTIVE_STATUSES` (SQL `.in_(list(ACTIVE_STATUSES))`), один источник множества.

**Frontend (`vts/static/app.js`, ~50 сравнений):**
- Прогресс-функции (1318-1386): `queued` остаётся явным; `waiting`/активные →
  через `statusPred.showsProgress`/обычный путь (vts-qzl уже починил семантику,
  теперь через предикат).
- Кнопки (1521-1535): `canPause`/`canResume` → `statusPred.canPause(status)` и
  т.д.; `canArchive` → `statusPred.canArchive`; restart-кнопки →
  `taskCap.canRestartSummary/canRestartFinalSummary(runtime)`.
- Терминальные ветки (1218, 1319, 1358, 2632, 2649): где семантика «завершена»
  → `statusPred.isFinished`; где «completed/failed показывает финальный вид» —
  оставить явными, если это не «finished» (напр. completed-специфичный рендер).
- Активные (1238, 1572, 1595, 2642, 2729, 2758): → `statusPred.isActive`.

Точное отображение каждого `===` на предикат — в плане реализации (пофайлово,
по номерам строк). Где сравнение реально про КОНКРЕТНЫЙ статус (не про группу) —
оставить `=== "completed"` и т.п.; предикаты только для семантических ГРУПП.

## Тестирование

- **Python-предикаты (`tests/test_task_status.py`, новый):** параметризованный
  тест по всем 8 статусам для каждого предиката — таблица истинности,
  фиксирующая текущее множество. Это контракт behavior-preserving.
- **Backend-переключение:** после каждой точки — полный `pytest` зелёный.
  Существующие тесты (`test_task_transitions`, MCP-wait, can_pause/resume) должны
  остаться зелёными БЕЗ изменений (предикаты возвращают те же значения).
- **API capabilities:** тест сериализации — `capabilities.can_restart_summary`/
  `can_restart_final_summary` для completed/failed задач с нужными шагами.
- **Статус-карта:** тест, что config-эндпоинт отдаёт `status_flags` со всеми 8
  статусами и верными флагами (сверка с `status_flags()`).
- **Frontend:** `verifier-web` — сценарий: кнопки pause/resume/archive/restart и
  прогресс-бары корректны для waiting/queued/running/completed/paused/failed.
  Плюс `node --check`. (JS-unit-фреймворка нет — покрытие через verifier-web.)

## Порядок реализации

1. Python-модуль `task_status.py` + множества-константы + unit-тесты (таблицы
   истинности). Ничего не переключаем.
2. Backend-переключение: main.py (pause/resume/archive), processor skip, MCP
   terminal, repo requeue → предикаты/константы. Полный сьют зелёный.
3. API: `capabilities` (restart) в TaskOut/TaskCompactOut; `status_flags` в
   config-эндпоинт. Тесты сериализации + карты.
4. JS-модуль `status-predicates.js` + подключение в index.html перед app.js;
   загрузка карты при bootstrap.
5. Frontend-переключение: ~50 `===` в app.js → предикаты. verifier-web зелёный.
6. Финал: бамп версии; полный сьют + verifier-web; grep-гард
   (`grep 'baseStatus === "' app.js` — только реально-конкретные статусы, не
   семантические группы).

## Вне объёма

- Исправление найденных расхождений-багов (waiting не-archivable; MCP-terminal
  без archived; любое иное, что вскроется при именовании) — отдельные bd-задачи.
- Изменение самого `TaskStatus` enum (значения, добавление/удаление).
- Рефакторинг `StepStatus`.
- Изменение поведения любого предиката относительно текущего множества.
