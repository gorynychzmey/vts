# Универсальная доставка результатов VTS во внешние системы

**Дата:** 2026-07-30
**bd:** vts-ouq
**Статус:** дизайн утверждён, ожидает ревью перед планом реализации

## Мотивация

Есть детерминированный пайплайн: страницы из Outline прокидываются в Cognee. Нужно добавить
в него видео: ссылка на видео → VTS транскрибирует → транскрипт попадает в Outline → оттуда в Cognee.

Связывать это через агента (claude.ai кидает ссылку, поллит готовность, тянет транскрипт, кладёт в
Outline через MCP) неверно: в принципиально детерминированном пайплайне не нужна агентная логика в
середине. Нужна интеграция VTS → внешняя система.

VTS — универсальный сервис транскрибации. Делать ему интеграцию с одним Outline неправильно. Нужен
**универсальный механизм доставки** транскрипта (сырого / обработанного / summary — на выбор) во
внешние системы, при котором базовый VTS остаётся чистым транскрибатором, а поддержка конкретных
систем добавляется расширением.

## Ключевые решения (из брейншторма)

| Развилка | Решение |
|---|---|
| Механизм | **Plugin-адаптеры**: ядро определяет контракт, адаптеры — отдельные пакеты. Ядро не знает про Outline. |
| Граница плагина | **In-process Python entry points** (`entry_points("vts.delivery")`). Установка pip-ом в тот же образ. |
| Точка вызова | **Post-completion hook** (рядом с `send_push_safe`). Доставка НЕ может провалить транскрибацию. |
| Надёжность | **Отдельная durable-очередь** (`DeliveryAttempt` в БД + consumer-loop в worker). At-least-once. |
| Конфиг направления | **Per-user `DeliveryTarget`** (сущность в БД VTS): адаптер + config + секреты. Задача ссылается по имени. |
| Секреты | **Шифрование at-rest** (Fernet, ключ `VTS_SECRETS_KEY`). Write-only через API/MCP. |
| Управление targets | **REST + MCP CRUD** (паритет с prompts/presets, общий repo). |
| Scope targets | **Только user-scoped** (`user_id NOT NULL`). System-level не нужен (снимает вопрос «чьи секреты у общего»). |
| Формат для адаптера | **Ядро даёт сырьё** (variant + content + метаданные), адаптер форматирует сам. |
| Кратность | **Список назначений** на задачу (N доставок → N строк в очереди, каждая со своим статусом). |
| В пресетах | `delivery` — поле пресета (только имена targets, без секретов). |
| Приоритет preset vs submit | **Submit переопределяет** (replace целиком по полю `delivery`), как остальные опции. |
| MVP-адаптер | **Один реальный — Outline.** |

## Архитектура

```
┌─ Ядро VTS (не знает про Outline) ──────────────────────────────┐
│  1. Контракт DeliveryAdapter (Protocol) + DeliveryPayload       │
│  2. Discovery: entry_points("vts.delivery")                     │
│  3. Delivery queue (DeliveryAttempt в БД) + consumer-loop        │
│  4. enqueue при completion (рядом с send_push_safe, deliver_safe)│
│  5. DeliveryTarget CRUD (REST+MCP), шифрованные секреты          │
│  6. delivery в submit/пресетах; get_delivery_status/retry        │
└─────────────────────────────────────────────────────────────────┘
         ▲ entry_point
┌────────┴──────────────────┐
│ vts-outline (плагин-пакет)│  свой Outline SDK, свой pyproject
│  OutlineAdapter           │  секреты берёт из target (расшифрованы ядром)
└───────────────────────────┘
```

Базовый VTS без установленных плагинов = чистый транскрибатор. `pip install vts-outline` → появляется
адаптер `outline`.

### Границы ответственности

- **Ядро**: «есть задача с результатом; доставить по списку назначений; вот сырьё; повтори при сбое;
  храни статус». Ничего про Outline. Резолвит variant → content, шифрует/расшифровывает секреты,
  ведёт очередь.
- **Адаптер**: как превратить сырьё в документ и положить в целевую систему. Тащит свой SDK. Секреты
  получает от ядра (расшифрованными, только в памяти) — сам их не хранит.
- **Задача** (через MCP/REST): выбирает target по имени + опциональный override несекретного (variant).
  Секреты через MCP не ходят.

## Контракт данных

```python
@dataclass(frozen=True)
class TaskMeta:
    source_url: str
    source_title: str | None
    language: str | None       # из options/артефактов
    duration_s: float | None
    created_at: datetime

@dataclass(frozen=True)
class DeliveryPayload:
    task_id: str
    variant: str               # "raw" | "redacted" | "summary"
    content: str               # уже прочитанный текст выбранного варианта
    content_format: str        # "txt" | "json" | "markdown"
    task: TaskMeta

@dataclass(frozen=True)
class DeliveryTargetConfig:
    config: dict[str, Any]     # несекретное: collection_id, base_url, ...
    secrets: dict[str, str]    # расшифровано перед вызовом, только в памяти

@dataclass(frozen=True)
class DeliveryResult:
    external_id: str | None    # id созданного документа (идемпотентность/ссылка)
    external_url: str | None

class DeliveryAdapter(Protocol):
    name: str                              # "outline"
    def config_schema(self) -> dict: ...   # JSON Schema для валидации target.config
    def secret_keys(self) -> list[str]: ...# ["api_token"] — что шифровать
    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult: ...               # успех → результат; сбой → raise (ядро ретраит)
```

- `config_schema` / `secret_keys` позволяют ядру **валидировать** target и знать, **что шифровать**,
  не зная самого адаптера.
- **Разрешение variant → content** — ответственность ядра (`vts/delivery/resolve.py`): читает
  `transcript_path` / `summary_path` / redacted-файл из `artifact_dir` тем же способом, что MCP
  `get_transcript`. Адаптер файловую систему не трогает.

## Схема данных

### DeliveryTarget (per-user, конфиг направления)

```python
class DeliveryTarget(Base):
    id: UUID
    user_id: UUID → users.id (ondelete=CASCADE, NOT NULL)
    name: str                  # уникально в пределах пользователя; ссылка из задач по имени
    adapter: str               # "outline"
    config_json: dict          # несекретное (открыто, может отдаваться в API)
    secrets_enc: bytes | None  # Fernet-шифрованный JSON {ключ: значение}
    created_at / updated_at
    UniqueConstraint(user_id, name)
```

### DeliveryAttempt (одна строка = одна доставка в один target)

```python
class DeliveryAttempt(Base):
    id: UUID
    task_id: UUID → tasks.id (ondelete=CASCADE)
    target_id: UUID → delivery_targets.id (ondelete=SET NULL)  # target могут удалить
    adapter: str               # снимок имени адаптера
    variant: str               # снимок: raw|redacted|summary
    status: DeliveryStatus     # pending|delivering|delivered|failed|dead
    attempts: int = 0
    max_attempts: int          # снимок настройки на момент enqueue
    next_attempt_at: datetime | None
    last_error: str | None
    external_id: str | None    # id документа в целевой системе (идемпотентность/ссылка)
    external_url: str | None
    created_at / updated_at
    Index(status, next_attempt_at)
```

Снимки (`adapter`, `variant`, `max_attempts`) — чтобы удаление/правка target не ломала интерпретацию
уже поставленной доставки.

## Поток доставки

### Enqueue (в `process_task`, сразу после `send_push_safe`)

Через `deliver_safe`-паттерн (весь блок в `try/except`, сбой логируется, задача остаётся `completed`):

1. Читает `options.delivery` — список `[{deliver_to: "outline-meetings", variant?}]`.
2. Для каждого элемента резолвит `DeliveryTarget` пользователя по имени; создаёт
   `DeliveryAttempt(status=pending, next_attempt_at=now)`; коммит.
3. `redis.publish(delivery:notify)` — будит consumer (паттерн `notify_queued`).

**Источник истины — БД-строка**, не Redis-сообщение. Redis только «будильник». Поэтому дырка в
графе знаний не возникает даже если publish не долетел: строка `pending` подхватится следующим тиком.

### Consumer loop (новый `asyncio.create_task` в `worker_loop`, рядом с `_upload_gc_loop`)

```
reaper: delivering-строки с updated_at старше порога → назад в pending  (реанимация после падения worker)
while True:
    claim = SELECT ... WHERE status=pending AND next_attempt_at<=now
            ORDER BY next_attempt_at LIMIT N FOR UPDATE SKIP LOCKED
    для каждого:
        status=delivering, attempts+=1, commit
        try:
            payload = resolve_variant_content(task)     # читает файл варианта
            target  = load_target + decrypt_secrets      # секрет только в памяти
            result  = adapter.deliver(payload, target)
            status=delivered, external_id/url, commit
        except:
            if attempts >= max_attempts: status=dead
            else: status=pending, next_attempt_at=now+backoff(attempts), last_error
    сон / ожидание delivery:notify (publish → wakeup.set, как queue:notify)
```

Решения:

- **`FOR UPDATE SKIP LOCKED`** — корректно при будущем multi-worker (доставки не задвоятся). Сегодня
  один worker, но модель сразу правильная.
- **At-least-once + идемпотентность в адаптере.** Если `external_id` уже есть (ретрай после того как
  документ создан, но статус не успел записаться), адаптер обновляет существующий документ, а не
  плодит дубль. `external_id` — крючок для этого.
- **Backoff** экспоненциальный с потолком, напр. `min(60·2^n, 3600)` сек; значения из настроек.
- **`dead`** после `max_attempts` — не молчаливая потеря: строка видна, `retry_delivery` оживляет
  (`status=pending, next_attempt_at=now`).

### Восстановление после рестарта

- `pending` с `next_attempt_at<=now` — подхватятся первым тиком.
- `delivering`, зависшие от убитого worker, — реаниматор по таймауту `updated_at` (аналог
  `recover_pending_tasks`).

## Поверхности

### DeliveryTarget CRUD (REST + MCP, общий repo)

REST (под существующей аутентификацией, user-scoped):

```
POST   /api/delivery-targets      {name, adapter, config, secrets}
GET    /api/delivery-targets      → список БЕЗ секретов (secrets: {key: {set: bool}})
GET    /api/delivery-targets/{id} → без секретов
PUT    /api/delivery-targets/{id} {name?, config?, secrets?}
DELETE /api/delivery-targets/{id}
```

MCP: `create_delivery_target`, `list_delivery_targets`, `update_delivery_target`,
`delete_delivery_target` — тонкие обёртки над тем же repo.

Инварианты (в repo/сервисе, не в хендлере, — общие для обеих поверхностей):

- **secrets write-only** — никогда не отдаются в ответах; в list/get вместо значения `{"api_token": {"set": true}}`.
- **update без секрета сохраняет старый**; явный `null`/`""` очищает.
- **валидация config** через `adapter.config_schema()` при create/update; неизвестный adapter
  (нет entry_point) → внятная ошибка (400 / MCP error).
- шифрование секретов при записи (`vts/core/secrets.py`); расшифровка только в consumer перед `deliver`.

### Submit с доставкой

Расширяем существующий MCP `submit_task` + REST create-task:

```
submit_task(url=..., ..., delivery=[
    {deliver_to: "outline-meetings"},           # variant из target.config.default_variant
    {deliver_to: "s3-archive", variant: "raw"}  # override несекретного
])
```

- Кладётся в `task.options.delivery` (список). Ссылка по **имени** target (не id) — агенту удобнее,
  имена стабильны, id он не знает.
- Валидация на submit: каждый `deliver_to` резолвится в существующий target пользователя;
  несуществующее имя → ошибка сразу (не тихо на completion).
- `variant`-override валидируется: `summary` требует включённого summarize-шага — иначе внятная
  ошибка на submit.

### Статус и ретрай доставки (для гарантии «долетело ли»)

```
MCP  get_delivery_status(task_id) → [{target, adapter, variant, status, attempts, external_url, last_error}]
MCP  retry_delivery(task_id, target?)              # dead/failed → pending
REST GET  /api/tasks/{id}/deliveries
REST POST /api/tasks/{id}/deliveries/{delivery_id}/retry
```

Закрывает Cognee-сценарий: пайплайн кидает ссылку с `delivery=[{deliver_to: "cognee-outline"}]`,
затем (если нужна гарантия перед следующим шагом) поллит `get_delivery_status` до `delivered` и
получает `external_url`. Без поллинга — доставка durable, ретраится сама, `dead` не теряется.

### SSE-событие (отложено, дёшево)

На смену статуса доставки — `delivery_status` через существующий `publish_event`. UI/агент реагируют
без поллинга. В дизайне заложено, реализация отложена.

## Пресеты

`delivery` — поле пресета (`User.default_preset` и именованные пресеты — та же система). Пресет
`"cognee-video"` = `{summarize: true, delivery: [{deliver_to: "cognee-outline"}], ...}`, и связка
«видео → транскрипт → Outline → Cognee» становится одним параметром `preset`.

**Две точки, требующие явной правки (легко пропустить):**

1. **`expand_preset_options`** (`vts/services/preset_expand.py`) — это allowlist известных полей.
   Новое поле `delivery` НЕ пройдёт автоматически — его нужно явно добавить в выборку, иначе оно
   молча выпадет.
2. **Слияние preset+submit** не централизовано: пресет расширяется в набор опций, submit-аргументы
   задают поля. Replace-семантика для `delivery` реализуется тем, что `delivery` из submit (если
   передан) кладётся в итоговые options вместо пресетного — ровно как `language`/`prompts`. Точку
   сборки указать явно.

`delivery` в пресете содержит только **имена** targets (без секретов) — пресеты остаются безопасными
для хранения и отдачи в API. Граница «имена в пресете, секреты в target» держится.

## Структура кода

Ядро (репозиторий vts):

```
vts/delivery/
  contract.py     # DeliveryAdapter Protocol, DeliveryPayload, DeliveryResult, TaskMeta, DeliveryTargetConfig
  registry.py     # discovery через entry_points("vts.delivery"); встроенных адаптеров нет
  resolve.py      # variant → content (raw/redacted/summary из artifact_dir)
  queue.py        # enqueue_deliveries(task), claim/backoff логика
  consumer.py     # delivery loop (запускается из worker_loop)
vts/core/secrets.py            # Fernet encrypt/decrypt, ключ VTS_SECRETS_KEY
vts/db/models.py               # +DeliveryTarget, +DeliveryAttempt, +DeliveryStatus
vts/db/repo.py                 # CRUD targets, claim/update attempts
vts/api/main.py, schemas.py    # REST: delivery-targets, task deliveries
vts/mcp/tools.py, schemas.py   # MCP: CRUD targets, submit.delivery, status, retry
vts/services/preset_expand.py  # +delivery в allowlist
alembic/                       # 2 миграции: delivery_targets, delivery_attempts
```

Плагин Outline — отдельный пакет `vts-outline/` (свой pyproject, свой Outline-клиент):

```
[project.entry-points."vts.delivery"]
outline = "vts_outline:OutlineAdapter"
```

`OutlineAdapter`: `config_schema` = `{base_url, collection_id, default_variant}`;
`secret_keys` = `["api_token"]`; `deliver` = сырьё → markdown (заголовок = source_title,
тело = content, метаданные) → create/update документа через Outline API; идемпотентность по
`external_id`. Устанавливается в тот же образ (Dockerfile: `pip install ./vts-outline`).

## Тестирование

- **Контракт/ядро** (без Outline): fake in-memory adapter, регистрируемый в тесте — enqueue → claim →
  deliver → delivered; backoff; dead после max_attempts; реанимация зависших `delivering`;
  идемпотентность повторной доставки.
- **Секреты**: encrypt/decrypt round-trip; write-only в API (GET не отдаёт); update без секрета
  сохраняет старый; явная очистка.
- **Пресеты**: `delivery` проходит `expand_preset_options`; submit-override заменяет пресетный `delivery`.
- **Outline-адаптер** (в его пакете): формирование markdown; идемпотентный update по `external_id` —
  против замоканного Outline API.
- Реальный Outline в CI не дёргаем.

## Границы MVP

Делаем сейчас:

- ✅ Контракт + discovery.
- ✅ `DeliveryTarget` (шифрованные секреты, REST+MCP CRUD, write-only секреты).
- ✅ `DeliveryAttempt` + очередь + consumer + backoff + dead + reaper.
- ✅ `delivery` в submit и пресетах (replace-семантика; `expand_preset_options` allowlist).
- ✅ `get_delivery_status` / `retry_delivery` (MCP + REST).
- ✅ Один реальный адаптер — **Outline**.

Отложено (в спеке как «дальше»):

- ⏸️ `delivery_status` SSE-событие (реализация).
- ⏸️ UI для управления targets.
- ⏸️ Дополнительные адаптеры (webhook, S3).
- ⏸️ Multi-worker нагрузка (модель готова через SKIP LOCKED, но не тестируем нагрузку).

## Открытые вопросы для реализации

- Точное имя/формат ключа `VTS_SECRETS_KEY` и поведение при его отсутствии (targets с секретами
  недоступны — fail loud на старте consumer, не молча).
- Значения по умолчанию для `max_attempts`, backoff-потолка, reaper-таймаута — в `Settings`.
- Формат `content_format` для summary (txt vs markdown) — от того, что реально пишет summarize-шаг.
