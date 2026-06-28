# Chunked upload for large files (>proxy limit)

**Задача:** vts-b8j (P2). Реальный инцидент: загрузка ~400 МБ обрывается фронтящим
прокси (Cloudflare/nginx) до того, как запрос дойдёт до backend — в логах нет ни
одного POST. Прокси-лимит вне нашего контроля (Cloudflare Free ~100 МБ).

## Цель

Дать загружать большие файлы (цель — 1 ГБ+) через веб-UI, разбивая тело на
маленькие чанки: каждый отдельный HTTP-запрос остаётся под прокси-лимитом.
Резюмируемость при обрыве сети. Существующий small-file flow остаётся
нетронутым.

## Решения (зафиксированы при брейнсторминге)

1. **TUS-style chunked + локальный диск.** Без object store — артефакты и так
   на диске под `artifacts_root`. Соответствует on-prem-модели (CLAUDE.md).
2. **Минимальный свой протокол**, не библиотечный TUS: 3 endpoint'а (init /
   patch chunk / finalize) + GET offset для резюма. Ноль новых зависимостей.
3. **Порог переключения 50 МБ, конфигурится с сервера.** Файлы ≤ порога → старый
   `POST /api/tasks/upload` (нетронут). Файлы > порога → chunked flow.
4. **Staging как `.part` в будущем task_dir**, метаданные в `upload.json`-сайдкаре,
   строка Task в БД создаётся ТОЛЬКО на finalize.
5. **Авторизация:** upload-сессия принадлежит эффективному `user.id` (с учётом
   имперсонации); чужой доступ → 403/404. Валидация suffix+опций на init.
   Лимит размера `max_upload_bytes` (дефолт 2 ГБ). PATCH сверяет offset → 409 при
   рассинхроне.

## Протокол (новые endpoint'ы)

Базовый префикс `/api/uploads`. Все требуют аутентификации (как существующие
per-user endpoint'ы), скоуп по `uuid.UUID(user.id)`.

### `POST /api/uploads/init`
Тело (JSON): `filename: str`, `total_size: int`, `options` (language, audio_only,
transcript, prompts, display_name — те же поля, что принимает `upload_task` через
Form). `display_name` сохраняется в `upload.json` и идёт в `source_title` на
finalize (через `normalize_display_name`).
- Валидирует suffix по `_ALLOWED_UPLOAD_SUFFIXES` (переиспользовать существующий
  frozenset — вынести из замыкания `create_app` в module-level константу
  `vts/api/uploads` или общий модуль, чтобы делить с `upload_task`).
- Валидирует опции (та же нормализация prompts, что в `upload_task`; prompts
  требуют transcript).
- `total_size <= 0` или `> settings.max_upload_bytes` → 413/422.
- Генерирует `upload_id = uuid4()` (он же будущий `task_id`).
- Создаёт `task_dir(artifacts_root, user.username, upload_id)` + `media/`.
- Пишет `<task_dir>/upload.json`: `{upload_id, user_id, username, suffix,
  total_size, received: 0, options, created_at}`.
- Создаёт пустой `<task_dir>/media/audio.original<suffix>.part`.
- Ответ: `{upload_id, chunk_size}` где `chunk_size` — рекомендованный размер
  чанка (конфиг `upload_chunk_bytes`, дефолт 8 МБ).

### `GET /api/uploads/{upload_id}/offset`
- Проверяет владельца (404 если сессии нет или чужая).
- Возвращает `{received: int, total_size: int}` — клиент досылает с `received`.
  Источник истины — фактический размер `.part` на диске (а не только
  `upload.json`), чтобы пережить аплоад, прерванный в середине записи чанка.

### `PATCH /api/uploads/{upload_id}?offset=N`
- Тело — сырые байты чанка (`Content-Type: application/offset+octet-stream`).
- Проверяет владельца (404).
- `N != current .part size` → 409 Conflict (клиент перечитывает offset и
  досылает). Это механизм резюма/устойчивости.
- `current_size + len(chunk) > total_size` → 413 (защита от переполнения).
- Аппендит чанк к `.part` (open append, `asyncio.to_thread`), обновляет
  `received` в `upload.json`.
- Ответ: `{received: int}` (новый размер).

### `POST /api/uploads/{upload_id}/finalize`
- Проверяет владельца (404).
- `.part` size `!= total_size` → 409 (незавершён).
- Атомарный `os.rename(.part → audio.original<suffix>)` (тот же том).
- Создаёт Task в БД ровно как `upload_task`: `source_url=f"file://{name}"`,
  `options` из `upload.json`, `artifact_dir=task_dir`, `task_id=upload_id`,
  `source_title=normalize_display_name(display_name)`.
- Enqueue: `bus.notify_queued()` + `publish_event(task_status)` — тот же tail,
  что `upload_task`. Выносим этот tail в переиспользуемый хелпер
  (`_enqueue_uploaded_task(...)`) и зовём из обоих мест (DRY).
- Удаляет `upload.json`.
- Ответ: `TaskOut` (как `upload_task`).

## Хранение и согласованность

- Staging и финал — на одном томе (`artifacts_root`), поэтому `rename` атомарен и
  мгновенен; копий между томами нет.
- Строка Task появляется только на finalize → брошенные/незавершённые загрузки
  не засоряют список задач.
- `upload.json` — единственное состояние сессии; новой таблицы в БД нет.
- **Очистка брошенных загрузок:** task_dir с `upload.json` старше TTL
  (`upload_session_ttl_seconds`, дефолт 24 ч) и без строки Task — мусор. В этом
  спеке: НЕ добавляем фоновый сборщик (out of scope, отдельная задача); finalize
  и так чистит `upload.json`. Брошенные `.part` остаются до ручной/будущей чистки.
  Зафиксировать как явный out-of-scope, не как недоделку.

## Конфиг (`vts/core/config.py`, `Settings`, env `VTS_`)

- `upload_chunked_threshold_bytes: int = 52_428_800` (50 МБ) — порог клиента.
- `upload_chunk_bytes: int = 8_388_608` (8 МБ) — рекомендованный размер чанка.
- `max_upload_bytes: int = 2_147_483_648` (2 ГБ) — потолок total_size.
- `upload_session_ttl_seconds: int = 86_400` — для будущей чистки (хранится,
  пока не используется сборщиком — задаём сразу, чтобы не плодить миграции конфига).

Порог отдаётся клиенту: новый `GET /api/uploads/config` →
`{chunked_threshold_bytes, chunk_bytes, max_upload_bytes}`. Клиент грузит в
`bootstrap()`.

## Клиент (`vts/static/app.js`)

В `createTask` для файла: если `file.size <= chunked_threshold_bytes` (или конфиг
не загрузился — фолбэк на старый путь) → текущий `uploadFileWithProgress(fd)`
(нетронут). Иначе → новый `uploadFileChunked(file, options)`:
1. `POST /api/uploads/init` с filename/total_size/options → `{upload_id, chunk_size}`.
2. Цикл по чанкам: `file.slice(offset, offset+chunk_size)` → `PATCH
   /api/uploads/{id}?offset=N`. На 409 — `GET offset`, скорректировать позицию,
   продолжить. На сетевую ошибку — повтор текущего чанка (несколько попыток с
   backoff), затем при необходимости резюм через `GET offset`.
3. Прогресс-бар: переиспользовать существующее кольцо (`submit-progress`,
   `setProgress(loaded/total)`) — обновляем после каждого чанка.
4. `POST /api/uploads/{id}/finalize` → `TaskOut`, дальше как сейчас (refresh).

Поведение small-file (≤ порога) для пользователя не меняется — тот же
single-shot POST.

## Безопасность

- Все upload-endpoint'ы скоупятся по эффективному `uuid.UUID(user.id)`
  (имперсонация работает как у `/api/prompts`). `upload.json.user_id` сверяется с
  текущим — чужая сессия → 404 (не раскрываем существование).
- Suffix и опции валидируются на init (не дать залить гигабайт ради 422 в конце).
- `max_upload_bytes` на init + переполнение-guard на PATCH.
- Path-safety: `upload_id` — это uuid из path; никогда не строим путь из
  пользовательского `filename` (берём только `suffix` и `Path(name).name` для
  `source_url`, как `upload_task` уже делает).

## Архитектура (границы единиц)

- **`vts/services/upload_session.py`** — чистая-ish логика сессии: чтение/запись
  `upload.json`, путь к `.part`, валидация offset, append-чанка, finalize-rename.
  Тестируется на временной директории без HTTP.
- **`vts/api/main.py`** (или новый `vts/api/upload_routes.py`) — 5 endpoint'ов,
  тонкие: auth + вызов сервиса + сериализация. Переиспользуют
  `_ALLOWED_UPLOAD_SUFFIXES` (вынесенный в module-level) и общий
  `_enqueue_uploaded_task` tail.
- **Клиент** — `uploadFileChunked` рядом с `uploadFileWithProgress`.

Если число endpoint'ов раздувает `create_app`, вынести upload-маршруты в
`vts/api/upload_routes.py` как APIRouter (следовать паттерну `auth_routes.py`).

## Тестирование

- `tests/test_upload_session.py` — сервис на tmp-директории: init создаёт
  структуру + sidecar; append растит `.part` и `received`; offset-mismatch
  detection; finalize переименовывает и собирает корректные байты; финальный файл
  == конкатенация чанков.
- `tests/test_uploads_api.py` (Postgres + httpx): полный happy-path (init →
  несколько PATCH → finalize → Task создан, статус queued); resume (GET offset
  после частичной загрузки, дослать остаток); 409 на неверный offset; 413 на
  превышение total_size/`max_upload_bytes`; 422 на плохой suffix на init;
  имперсонация/owner-isolation (чужой upload_id → 404); finalize незавершённого →
  409. small-file путь (`upload_task`) не задет.
- UI verifier: стаб `/api/uploads/*`; сценарий, что при файле > порога клиент
  идёт chunked-путём и доходит до finalize (можно проверить последовательность
  вызовов через стаб), а при ≤ порога — старый POST. Если гонять реальную
  нарезку в headless сложно — минимум проверить, что `loadUploadConfig` не ломает
  boot и порог-ветвление выбирает правильный путь по `file.size`.

## Изменения в коде (итог)

- **Новое:** `vts/services/upload_session.py`, `tests/test_upload_session.py`,
  `tests/test_uploads_api.py`, (опц.) `vts/api/upload_routes.py`, UI-сценарий.
- **Правка:** `vts/api/main.py` — вынести `_ALLOWED_UPLOAD_SUFFIXES` в
  module-level, выделить `_enqueue_uploaded_task` tail, добавить 5 endpoint'ов
  (init/offset/patch/finalize/config) или подключить router; `vts/core/config.py`
  (4 настройки); `vts/static/app.js` (`uploadFileChunked` + `loadUploadConfig` +
  порог-ветвление в `createTask`); `vts/__init__.py` (версия).
- **Без** новой таблицы БД, без object store, без новых пакетов.

## Out of scope
- Фоновый сборщик брошенных upload-сессий (TTL-чистка) — отдельная задача.
- Параллельная загрузка нескольких чанков одновременно (последовательно
  достаточно; резюм покрывает обрывы).
- Presigned/object-store путь.
- Увеличение прокси-лимита (вне нашего контроля).
