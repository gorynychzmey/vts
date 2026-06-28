# vts-hp7 — Шаблоны настроек задачи (task option presets)

**Status:** Design approved, ready for implementation plan
**Date:** 2026-06-28
**Beads:** vts-hp7
**Builds on:** custom prompts (VOS-63) — mirrors its system/user + duplicate + ref infrastructure.

## Проблема

Пользователь каждый раз заново выставляет опции создания задачи (язык,
audio-only, transcript, набор промптов). Цель: сохранять готовый набор опций
как именованный шаблон («пресет») и применять его при создании новой задачи.

## Концепция

Пресет — именованный набор **переиспользуемых** опций создания задачи:
`language`, `audio_only`, `transcript`, `prompts`. НЕ включает источник
(url/файл) — он per-task. Применение пресета заполняет форму создания этими
опциями; пользователю остаётся указать источник.

Зеркалит инфраструктуру промптов (VOS-63):

- **Системные пресеты** — read-only, описаны реестром в коде. Сейчас один:
  «Default» = текущее неявное поведение формы.
- **Пользовательские пресеты** — записи в БД на пользователя (CRUD +
  дублирование).
- Ссылка на пресет — составная `PresetRef {source: "system"|"user", id}`
  (id = ключ для system, UUID-строка для user), по аналогии с `PromptRef`.

**Дефолт:** у пользователя ровно один активный дефолтный пресет
(`users.default_preset`). Может указывать и на системный, и на
пользовательский пресет; `null` → системный «Default». Применяется к форме
при загрузке страницы. Пользователь свободно переключает дефолт (включая
обратно на системный).

## Модель данных

### Таблица `presets` (пользовательские)

```
id          UUID  PK
user_id     UUID  FK → users (ondelete CASCADE; приватны на пользователя)
name        Text
options     JSON  -- {language, audio_only, transcript, prompts: [PromptRef]}
created_at  timestamptz
updated_at  timestamptz
```

`options` shape:
```json
{ "language": null | "en"|..., "audio_only": false, "transcript": true,
  "prompts": [{"source": "system", "id": "summary"}, ...] }
```

### Колонка `users.default_preset`

Новая колонка `default_preset JSON, nullable`. Хранит `PresetRef`
(`{source, id}`) активного дефолта, или `null` → системный «Default».
(У `User` нет общего settings-JSON — добавляем отдельную колонку, по аналогии
с существующей `preferred_ytdlp_client`.)

### Реестр системных пресетов

Список в коде (как `SystemPromptDef` в `vts/services/prompt_registry.py`):
`SystemPresetDef {key, i18n_name_key, options}`. Сейчас один элемент:

```
{ key: "default", i18n_name_key: "preset.system.default",
  options: { language: null, audio_only: false, transcript: true,
             prompts: [{source: "system", id: "summary"}] } }
```

Несъёмный, нередактируемый из UI; можно дублировать и назначать дефолтным.
Расположение: новый модуль `vts/services/preset_registry.py` (рядом с
prompt_registry), с `SYSTEM_PRESETS`, `list_system_presets()`,
`parse_preset_ref()`/`preset_ref_to_dict()` (по образцу prompt_registry).

### Висячие промпт-ссылки

`options.prompts` хранит `PromptRef` списком. Пользователь мог удалить
пользовательский промпт, на который ссылается пресет. Правило:

- **Применение** пресета к форме РЕЗОЛВИТ `options.prompts` против актуального
  списка промптов и молча отбрасывает несуществующие пользовательские ссылки
  (системные ссылки всегда валидны). Пресет не ломается.
- **Подчистка** — явное действие в UI («Пересохранить», см. UI).

## API

> `PresetRef` — новая Pydantic-модель `{source: Literal["system","user"], id: str}`
> (зеркало `PromptRef`). `PresetOptions` — `{language: str|None, audio_only: bool,
> transcript: bool, prompts: list[PromptRef]}`.

### HTTP

- `GET /api/presets` — объединённый список **system + user**. Каждый:
  `{source, id, name, options, editable}`. Системные — имя локализовано под
  язык клиента (как у промптов: фронт резолвит по ключу — см. ниже). Источник
  для дропдауна формы и диалога управления.
- `POST /api/presets` — создать пользовательский (`name`, `options`).
- `PATCH /api/presets/{preset_id}` — редактировать пользовательский
  (`name?`, `options?`). 404 если не найден/не владелец.
- `DELETE /api/presets/{preset_id}` — удалить пользовательский. **Если
  удаляемый был активным дефолтом пользователя → откат дефолта на системный
  «Default»** (если системных пресетов станет несколько — на первый из
  реестра). 404 если не найден/не владелец.
- `GET /api/me/default_preset` → `{source, id}` (текущий активный дефолт;
  системный, если не задан).
- `PUT /api/me/default_preset` — назначить активный дефолт; тело
  `{source, id}`. Принимает и системный, и пользовательский ref; 422/404 на
  невалидный/несуществующий.
- **Дублирование** — клиентом: `POST /api/presets` с предзаполненными
  `name + " (copy)"` и `options` существующего (включая системного). Отдельного
  эндпоинта не нужно (как у промптов).

**Локализация имени системного пресета** (как vts-mqk для промптов): сервер
возвращает английский `name` (`display_name` из реестра); web UI локализует по
`id` (ключ `preset.system.${id}`), внешние клиенты получают английское имя.
`SystemPresetDef` несёт `display_name` + `i18n_name_key`.

**Применение пресета — чисто клиентское.** Дропдаун отдаёт `options`, фронт
заполняет форму. **`TaskCreateRequest` НЕ меняется** — пресет не передаётся на
создание задачи; он разворачивается в опции на клиенте. Сервер при создании
задачи о пресетах не знает.

### MCP

Зеркалит HTTP-CRUD + добавляет применение пресета на сервере (т.к. MCP создаёт
задачи без UI):

- Новые tools: `list_presets`, `create_preset`, `update_preset`,
  `delete_preset`, `get_default_preset`, `set_default_preset`.
- `submit_video` / `create_task` получают опциональный параметр
  `preset: PresetRef | None`. Если задан: сервер резолвит пресет, разворачивает
  его `options` как БАЗУ, а явно переданные поля (`language`, `transcript`,
  `audio_only`, `prompts`) ПЕРЕОПРЕДЕЛЯЮТ соответствующие поля пресета.
  Висячие промпт-ссылки пресета отфильтровываются при развороте.
  (В UI разворачивание делает фронт; в MCP — сервер.)

## UI

### Форма создания

- **Дропдаун выбора пресета** — слева/выше списка опций
  ([index.html](../../../vts/static/index.html) — секция new-task). Список из
  `GET /api/presets` (системные + пользовательские с пометкой). При загрузке
  выбран активный дефолт (`GET /api/me/default_preset`), форма заполнена его
  `options`. Выбор другого пресета → форма перезаполняется его опциями.
- **«Грязное» состояние:** после выбора пресета любое изменение опции
  (`language`/`audio_only`/`transcript`/`prompts`) помечает форму изменённой
  относительно выбранного пресета.
- **Одна кнопка сохранения, три состояния:**
  - пресет не выбран ИЛИ выбран-и-не-изменён → **«Сохранить как пресет»**
    (создать новый из текущих опций; запросить имя).
  - выбран **пользовательский** пресет И изменён → **«Сохранить изменения»**
    (перезаписать выбранный, `PATCH`).
  - выбран **системный** пресет И изменён → только **«Сохранить как пресет»**
    (системный нельзя перезаписать).

### Диалог управления пресетами

Как диалог промптов (`tokens-dialog`):

- Список: системные (read-only) + пользовательские.
- Действия: для пользовательских — редактировать (имя + опции) / удалить; на
  ВСЕХ — **дублировать** (форк; системный → пользовательский, имя «… (copy)»)
  и **«Сделать дефолтным»** (вызывает `PUT /api/me/default_preset`).
- Редактирование опций пресета — мини-набор контролов: language / audio_only /
  transcript / мультиселект промптов (переиспользуем `renderPromptMultiselect`).

### Висячие промпты в пресете

При выборе пресета, чьи `options.prompts` содержат ссылки на несуществующие
пользовательские промпты — ненавязчивая подсказка «В пресете есть удалённые
промпты» + кнопка **«Пересохранить»** (перезаписывает пресет
отфильтрованными `options.prompts` через `PATCH`). Применение к форме в любом
случае игнорирует висячие ссылки.

### i18n

Новые ключи во всех трёх локалях (`vts/static/i18n/{en,ru,de}.js`):
`preset.system.default`, `new_task.preset` (лейбл дропдауна),
`preset.save_as`, `preset.save_changes`, `preset.manage.*` (title/create/edit/
delete/duplicate/make_default/name/...), `preset.dangling_prompts` (подсказка),
`preset.resave` («Пересохранить»), `preset.copy_suffix`.

Переиспользуем существующие паттерны: `tokens-dialog`, `renderPromptMultiselect`.

## Тестирование

- Реестр + ref-хелперы (preset_registry): `parse_preset_ref`, системный
  «default» присутствует с правильными options.
- Repo CRUD пресетов + изоляция между пользователями.
- `users.default_preset`: чтение/запись; откат на системный при удалении
  дефолтного пресета.
- HTTP: CRUD, GET/PUT default_preset (system + user refs, 404), list включает
  системный «default» (editable=false).
- Висячие-промпт фильтрация при развороте (unit).
- MCP: list/create/update/delete presets; default get/set; `submit_video`
  с `preset` разворачивает options + явные поля переопределяют.
- Прогон через authed-client харнесс (`tests/conftest.py`) на реальном Postgres.
- **UI:** verifier-web сценарий для дропдауна пресетов + диалога (после
  реализации) — closed-state и применение.

## Out of scope

- Шаринг пресетов между пользователями.
- Несколько системных пресетов (реестр поддерживает, но сейчас один «Default»).
- `preset` в HTTP `TaskCreateRequest` (только MCP; UI разворачивает на клиенте).
