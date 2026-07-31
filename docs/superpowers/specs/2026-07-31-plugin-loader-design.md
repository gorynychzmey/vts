# Загрузчик плагинов-адаптеров доставки VTS + версионирование контракта

**Дата:** 2026-07-31
**bd:** vts-ouq (родительская фича доставки); этой фиче — завести отдельный issue при переходе к плану
**Статус:** дизайн утверждён, ожидает ревью перед планом реализации
**Предусловие реализации:** kube-миграция **vts-0pg** (initContainer как дом для bootstrap) — делается ПЕРЕД реализацией.

## Мотивация

Ядро доставки VTS (spec 2026-07-30-delivery-adapters) обнаруживает адаптеры через
`entry_points("vts.delivery")` — ядро не содержит ни одного адаптера и не знает про конкретные внешние
системы. Открытый вопрос: **как и откуда адаптеры попадают в окружение VTS**.

Утверждённая модель установки: отдельный GitHub-репозиторий (или несколько) с плагинами; VTS на старте
тянет готовые wheel'ы и ставит их так, чтобы entry-point discovery их подхватил. Базовый образ VTS без
сконфигурированных источников = чистый транскрибатор без адаптеров.

Вторая, связанная потребность: гарантировать, что тянущийся `latest` плагин **совместим** с текущим
контрактом ядра — иначе несовместимый адаптер упадёт в рантайме внутри `deliver()`. Отсюда
версионирование контракта + проверка совместимости при загрузке.

## Границы (что в scope этой спеки, что нет)

Фича состоит из **двух связанных единиц внутри VTS** + **одного ТЗ наружу**:

- **A. Loader** (`vts/delivery/loader.py`) — «достать и установить». В scope.
- **B. Contract versioning + load-time validation** (`vts/delivery/contract.py` + `registry.py`) — «проверить
  совместимость загруженного». В scope.
- **C. ТЗ репо плагинов** — контракт наружу для отдельного проекта. В scope как раздел-ТЗ; реализация — нет.

**Явно ВНЕ scope** (отдельными единицами работы):

- CI репо плагинов + триггер от пересборки VTS — отдельный проект (репо плагинов + workflow VTS). Здесь
  только не-нормативная рекомендация.
- Сам репо плагинов и его наполнение — отдельный проект.
- Миграция деплой-топологии на `podman kube` — bd **vts-0pg**. Спека формулирует требование к топологии
  абстрактно, не диктует механизм.

## Ключевые решения (из брейншторма)

| Развилка | Решение |
|---|---|
| Артефакт | **Wheel-файлы** (`.whl`), не исходники. Без сборки/доверия к произвольному коду в контейнере. |
| Источник | **GitHub Releases**: latest release, его `.whl`-assets. |
| Версия | **latest** (осознанный компромисс: удобство > детерминизм образа; host-cache сглаживает). |
| Дедуп skip/update | **По digest/etag asset'а** (устойчиво к пересборке той же версии). |
| Доступ | **private + public**; GitHub-токен **опционален** (имя env-переменной в конфиге, не сам токен). |
| Несколько источников | **Да** — список источников (на будущее для сторонних адаптеров). |
| Установка | `pip install --target=<host-cache>/site` **`--no-deps`**; `site/` на `PYTHONPATH`. |
| Когда | **Отдельный bootstrap-шаг** до старта api и worker (в kube — initContainer). |
| При сбое загрузки | **Деградировать** пер-источник: лог + использовать что в кэше; VTS стартует; exit 0. |
| Версионирование контракта | **min-compatible**: `CONTRACT_VERSION=(major,minor)`; плагин объявляет минимум. |
| Правило загрузки | `plugin.major == core.major AND plugin.minor <= core.minor` + Protocol-conformance. |
| Несовместимый плагин | **Не в реестре, но зафиксирован с причиной** (видимость оператору). |

## Архитектура

```
┌─ Bootstrap (отдельный шаг/процесс: python -m vts.delivery.loader) ──────┐
│  A. Loader: sources → GitHub Releases latest → .whl assets →            │
│     digest-skip vs manifest → download+verify → pip install --target    │
│     → update manifest. Сеть, изолирован, деградирует. НЕ знает про       │
│     адаптеры/контракт.                                                   │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ пишет в host-cache/site/ (том)
                               ▼
     ┌── host-cache (смонтированный том) ──┐
     │  site/          ← на PYTHONPATH     │
     │  manifest.json  ← digest по asset   │
     │  .lock                              │
     └──────────────┬──────────────────────┘
                    │ PYTHONPATH
        ┌───────────┴───────────┐
        ▼                       ▼
┌─ api ──────────┐      ┌─ worker ───────┐
│ registry +     │      │ registry +     │
│ B: contract    │      │ B: contract    │
│ validation при │      │ validation при │
│ discovery      │      │ discovery      │
└────────────────┘      └────────────────┘
```

**Граница A ⟂ B:** A ставит пакеты, B валидирует то, что Python в итоге загрузил. Связывает их только
существующий `entry_points`-discovery в registry. B работает без A (вручную-вшитый в образ адаптер тоже
валидируется); A работает без B. Слабая связанность.

## A. Loader

### Конфигурация (в `Settings`, yaml + env)

```yaml
delivery_plugin_sources:
  - repo: "owner/vts-plugins"          # GitHub repo owner/name
    token_env: "VTS_PLUGIN_TOKEN_MAIN" # имя env-переменной с токеном; пусто = публичный
  - repo: "thirdparty/their-adapters"
    token_env: ""
delivery_plugin_cache_dir: "/opt/vts/state/plugins"   # host-cache (смонтирован как том)
delivery_plugin_contract_strict: false                # относится к B (валидация), не к A; вынесено в общий
                                                      # конфиг: hard-skip vs мягкий reject несовместимых
```

- **Имя env-переменной токена** в yaml, не токен — секрет остаётся в host env-файле (`<project>.env`).
- env-форма списка источников — CSV/JSON по существующему паттерну `_CsvEnvSource` (`vts/core/config.py`).
- Пусто (`delivery_plugin_sources: []`) → loader ничего не ставит.

### Layout host-cache

```
/opt/vts/state/plugins/
  site/                     # pip install --target здесь; ЕДИНСТВЕННЫЙ каталог на PYTHONPATH
    vts_outline/  ...       # пакеты + .dist-info (entry_points)
  manifest.json             # {asset_name: {digest, version, source_repo, installed_at}}
  .lock                     # файловая блокировка на время установки
```

`manifest.json` — источник истины для skip/update, **keyed by asset digest**, не по имени файла.

### Поток (bootstrap)

```
load Settings; sources = settings.delivery_plugin_sources
if not sources: log "no plugin sources"; exit 0
acquire .lock (timeout; занят → log & exit 0)
manifest = read manifest.json or {}
for source in sources:
    try:
        release = github_latest_release(source.repo, token=env.get(source.token_env))
        assets  = [a for a in release.assets if a.name.endswith(".whl")]
        for asset in assets:
            if manifest.get(asset.name, {}).get("digest") == asset.digest:
                continue                                   # skip-if-present
            wheel = download(asset.url, token)  → tmp
            assert sha256(wheel) == asset.digest           # целостность
            pip install --target=site/ --no-deps wheel
            manifest[asset.name] = {digest, version, source_repo, installed_at}
    except (network/auth/api/install) as e:
        log.error("plugin source %s failed: %s — using cache", source.repo, e)
        continue                                           # ДЕГРАДАЦИЯ пер-источник
write manifest.json (atomically)
release .lock
exit 0                                                     # exit≠0 только на внутренней ошибке
                                                           # (нет прав на cache-dir, нечитаемый конфиг)
```

Решения:

- **`--no-deps`.** Wheel самодостаточен относительно окружения VTS (зависит только от `vts` + того, что
  уже в образе). Транзитивные зависимости из сети в host-cache = риск конфликта версий с образом + лишняя
  точка отказа. Экзотику плагин вендорит. Осознанное ограничение ради детерминизма → в ТЗ репо плагинов.
- **Деградация пер-источник, не всё-или-ничего.** Один недоступный источник не мешает остальным.
- **exit 0 при сетевых сбоях.** Bootstrap не роняет старт пода из-за GitHub/сети (иначе initContainer-fail →
  под не встаёт → падает и транскрибация). Фатальный exit только на операторски-чинимом (права, конфиг).
- **Целостность:** скачали → сверили sha256 с digest из Releases API → только потом install.
- **tmp → install → manifest:** запись в manifest только после успешного install; прерывание не оставляет
  «полу-установленного» в manifest. Частичный пакет в `site/` самоисцеляется: следующий старт видит
  digest-mismatch и `pip install --target` перезаписывает идемпотентно.

### Точка запуска в топологии (абстрактно)

Bootstrap ДОЛЖЕН выполняться **до** старта api и worker и писать в **общий с ними том** (host-cache).
Механизм зависит от топологии и НЕ фиксируется этой спекой:
- текущая (два systemd-юнита) — oneshot-юнит с `After=`/`Requires=` у api/worker;
- будущая (`podman kube`, vts-0pg) — **initContainer** (каноничный «до основных, общий том»).

Реализация делается уже под kube-композицию (vts-0pg — предусловие).

## B. Contract versioning + load-time validation

### Контракт (`vts/delivery/contract.py`)

```python
CONTRACT_VERSION = (1, 0)   # (major, minor); эволюция — см. правило ниже
```

Адаптер объявляет **минимально требуемую** версию:

```python
class DeliveryAdapter(Protocol):
    name: str
    contract_version: tuple[int, int]   # минимум (major, minor), который нужен адаптеру
    def config_schema(self) -> dict: ...
    def secret_keys(self) -> list[str]: ...
    async def deliver(self, payload, target): ...
```

### Валидация при загрузке (`vts/delivery/registry.py`)

```
для каждого entry_point группы vts.delivery:
    try:
        adapter = ep.load()()                                  # инстанцируем
        if not isinstance(adapter, DeliveryAdapter):           # runtime_checkable
            reject(ep.name, "не реализует контракт"); continue
        v = getattr(adapter, "contract_version", None)
        if v is None:
            reject(ep.name, "не объявляет contract_version"); continue
        if v[0] != CONTRACT_VERSION[0]:
            reject(adapter.name, f"major {v[0]} ≠ ядро {CONTRACT_VERSION[0]}"); continue
        if v[1] > CONTRACT_VERSION[1]:
            reject(adapter.name, f"нужен minor ≥{v[1]}, ядро {CONTRACT_VERSION[1]}"); continue
        registry[adapter.name] = adapter
    except Exception as e:
        reject(ep.name, f"загрузка упала: {e}")
```

- **min-compatible:** грузим iff `plugin.major == core.major AND plugin.minor <= core.minor`.
- **reject = не в реестре, но записан в `incompatible_adapters` с причиной** — видимость оператору (и
  будущему `GET /api/delivery-adapters`). Не молчаливое исчезновение.
- **Изоляция сбоя:** падение одного `ep.load()` не сносит discovery остальных.
- `delivery_plugin_contract_strict` (конфиг): по умолчанию несовместимый → reject+лог; strict — можно
  сделать фатальным на bootstrap (опционально; по умолчанию мягко, чтобы не ронять ядро).

**Как это закрывает latest-дыру:** loader тянет latest wheel; если он собран против несовместимого
контракта (ядро обновили, плагин нет), B ловит это на загрузке и не пускает адаптер в работу — вместо
загадочного падения `deliver()` оператор видит «outline: major 1 ≠ ядро 2». Это прод-страховка, которую
CI-триггер (вне scope) не даёт, т.к. `latest`-биндинг происходит на рестарте, а не на сборке.

### Правило эволюции контракта (явное проектное решение на будущее)

Действует для ВСЕХ будущих изменений `vts/delivery/contract.py`:

- **Удаление/переименование поля или метода, смена семантики/сигнатуры → bump MAJOR.** Breaking; старые
  адаптеры перестают загружаться (осознанно, с внятной причиной).
- **Только ДОБАВЛЕНИЕ нового (опционального) поля/метода → bump MINOR.** Обратно-совместимо; старые
  адаптеры продолжают грузиться.
- **Инвариант:** в пределах одного major — только обратно-совместимые добавления. Иначе min-compatible
  становится ложью.
- **При MAJOR-bump — ревизия КОДА плагинов, не только пересборка:** breaking-изменение может затронуть
  логику адаптера, а не только сигнатуры. Каждый адаптер ревьюится против нового контракта.

### Стыковка с уже сделанным ядром

`contract.py` написан в Tasks 1–8 (vts-ouq) БЕЗ `CONTRACT_VERSION`/`contract_version`. Добавление
обратно-совместимо (константа + Protocol-атрибут). `vts-outline` (Task 13 плана доставки) должен объявить
`contract_version = (1, 0)`.

## C. ТЗ репо плагинов (контракт наружу — для отдельного проекта)

Что VTS ожидает от репозитория плагинов:

1. Плагины публикуются как **GitHub Releases**; каждый релиз несёт один или несколько `.whl` как **release
   assets**.
2. Loader берёт **latest release** и ставит все его `.whl`-assets.
3. Каждый wheel **самодостаточен** относительно окружения VTS: зависит только от `vts` (+ уже присутствующее
   в образе VTS); экзотические зависимости **вендорятся** (следствие `--no-deps`).
4. Каждый адаптер объявляет: `name`, `contract_version=(major,minor)`, entry point в группе `vts.delivery`.
5. Приватный репо → токен с правом чтения releases; публичный → без токена.
6. **(Не-нормативно, рекомендация)** У репо плагинов — свой CI. VTS триггерит этот CI **при любом изменении
   контракта** (`vts/delivery/contract.py` / `CONTRACT_VERSION`), major ИЛИ minor — не только major и не на
   каждый коммит VTS. Причина: триггер только на major автоматизирует наименее нужный случай (плагин с
   чужим major и так не загрузится — B ловит), а пропуск minor упустил бы опасный случай — случайный
   breaking под видом minor. Точную формулировку триггера финализирует проект репо плагинов.

## Тестирование

- **Loader (A), без сети:** GitHub Releases API + скачивание за интерфейсом (мок через `respx`). Кейсы:
  пустой конфиг → ничего, exit 0; новый digest → download+install+manifest; совпавший digest → skip (без
  скачивания); недоступный источник → лог+деградация, остальные ставятся; digest-mismatch → не ставит, не
  пишет manifest; `.lock` занят → ранний выход. Установка `--target` — на локальном самодельном wheel, без
  сети.
- **Валидация (B):** fake-адаптеры в тесте (как в существующих registry-тестах): совместимый major/minor →
  в реестре; больший требуемый minor → reject с причиной; чужой major → reject; нет `contract_version` →
  reject; `ep.load()` бросает → reject, остальные грузятся; несовместимые видны в `incompatible_adapters`.
- **Реального GitHub в CI не дёргаем** (принцип «реальный Outline не дёргаем»).

## Границы MVP

**Делаем сейчас:**

- ✅ Loader (A): источники, manifest+digest-skip, деградация пер-источник, `--no-deps`, `.lock`, целостность.
- ✅ `CONTRACT_VERSION` + min-compatible валидация (B) с видимостью несовместимых.
- ✅ Конфиг в `Settings`; bootstrap-команда `python -m vts.delivery.loader`.
- ✅ ТЗ репо плагинов (раздел C).
- ✅ Правило эволюции контракта — в проектные правила.

**Отложено (в спеке как «дальше»):**

- ⏸️ `GET /api/delivery-adapters` + MCP `list_delivery_adapters` (видимость установленных/несовместимых) —
  дёшево, но отдельно.
- ⏸️ CI-триггер репо плагинов — отдельный проект.
- ⏸️ Сам репо плагинов и его наполнение — отдельный проект.
- ⏸️ Горячая перезагрузка плагинов без рестарта — не нужна (рестарт пода дёшев).

**Предусловие реализации:** kube-миграция **vts-0pg** (initContainer как дом для bootstrap) — перед
реализацией.

## Открытые вопросы для реализации

- Точный digest-источник из GitHub Releases API (asset `digest`/`sha256` vs собственный расчёт после
  скачивания) — свериться с актуальным ответом API при реализации; целостность в любом случае считаем сами.
- Как именно api/worker получают `site/` в `PYTHONPATH` в kube-топологии (env пода vs `.pth`) — решается
  вместе с vts-0pg.
- Поведение `delivery_plugin_contract_strict` в проде по умолчанию (мягко) — подтвердить при реализации.
- Ротация/очистка старых версий в `site/` при обновлении (pip `--target` перезапись vs накопление) —
  уточнить, нужна ли явная очистка.
