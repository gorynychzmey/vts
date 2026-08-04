# План реализации: загрузчик плагинов доставки + версионирование контракта (vts-9y7)

**Спека:** `docs/superpowers/specs/2026-07-31-plugin-loader-design.md` (утверждена)
**bd:** vts-9y7 (часть B — версионирование), vts-j8gz (часть A — сам loader)
**Предусловие:** vts-0pg — ✅ закрыт 2026-08-02, прод уже на `podman kube` (под `deploy/vts.yaml`).

**Статус:** часть B (задачи 1–3, 7) — ✅ СДЕЛАНО 2026-08-04. Часть A (задачи 4–6) — отложена в
vts-j8gz: реальный `vts-outline` лежит в дереве репозитория, внешнего репо плагинов ещё нет,
значит `delivery_plugin_sources` пуст и loader на проде был бы no-op. Решение Victor.

## Что уже есть (проверено в коде, не предполагается)

- `vts/delivery/registry.py` — discovery через `entry_points(group="vts.delivery")`, кэш `_CACHE`,
  `UnknownAdapter`. Валидации нет вообще: `ep.load()()` и в словарь.
- `vts/delivery/contract.py` — `DeliveryAdapter` Protocol (`runtime_checkable`), БЕЗ `CONTRACT_VERSION`
  и без `contract_version` у Protocol.
- `vts-outline/vts_outline/__init__.py` — реальный адаптер лежит в этом же дереве (не во внешнем репо),
  объявляет `name = "outline"`, `contract_version` НЕ объявляет.
- `deploy/vts.yaml` — под с initContainer `migrate`; в комментарии прямо заложено место для второго
  initContainer этой задачи.
- `docker/vts-entrypoint.sh` — диспетчер ролей (`webapi|worker|both|migrate`), роль = env `VTS_ROLE`.
- `vts/core/config.py` — `Settings` (pydantic-settings), `_CsvEnvSource` для CSV-списков в env,
  yaml-оверрайды из `/opt/vts/config/config.yaml`.
- `vts/api/main.py` — `create_delivery_target_endpoint` уже валидирует адаптер через `get_adapter()`,
  то есть недоступный адаптер отсекается на создании таргета.

## Порядок задач

B (версионирование) идёт ПЕРЕД A (loader): B самодостаточен, тестируется без сети и без установки,
и именно он закрывает latest-дыру. A без B тоже работает, но смысла в нём меньше.

---

### Задача 1 — `CONTRACT_VERSION` + `contract_version` в контракте

`vts/delivery/contract.py`:
- Добавить `CONTRACT_VERSION = (1, 0)`.
- Добавить `contract_version: tuple[int, int]` в Protocol `DeliveryAdapter`.

**Осторожно:** `DeliveryAdapter` — `runtime_checkable`. `isinstance()` для Protocol проверяет только
НАЛИЧИЕ атрибутов/методов, не их типы и не сигнатуры. Добавление атрибута в Protocol означает, что
адаптер без `contract_version` перестанет проходить `isinstance` — это ровно то, чего мы хотим, но
проверить надо явным тестом, а не поверить.

**Тесты:** `CONTRACT_VERSION` существует и имеет форму `(int, int)`; fake-адаптер без
`contract_version` не проходит `isinstance(..., DeliveryAdapter)`.

---

### Задача 2 — валидация при загрузке в registry

`vts/delivery/registry.py`. Переписать `_load_from_entry_points` по алгоритму спеки:

```
for ep in entry_points(group="vts.delivery"):
    try:
        adapter = ep.load()()
        if not isinstance(adapter, DeliveryAdapter):   reject(ep.name, "не реализует контракт")
        v = getattr(adapter, "contract_version", None)
        if v is None:                                   reject(ep.name, "не объявляет contract_version")
        if v[0] != CONTRACT_VERSION[0]:                 reject(adapter.name, "major mismatch")
        if v[1] > CONTRACT_VERSION[1]:                  reject(adapter.name, "нужен minor >= ...")
        registry[adapter.name] = adapter
    except Exception as e:                              reject(ep.name, f"загрузка упала: {e}")
```

- Добавить `incompatible_adapters() -> dict[str, str]` (имя → причина) + свой кэш, сбрасываемый
  вместе с `_CACHE`.
- Отклонённый адаптер НЕ попадает в реестр, но виден с причиной; лог `warning` на каждый reject.
- Изоляция: исключение на одном ep не должно ронять discovery остальных.

**Осторожно:** `v[0]`/`v[1]` на мусорном значении (строка, короткий кортеж, None-элементы) кинет
TypeError/IndexError внутри try → уйдёт в общий `except` с невнятной причиной. Нужна явная проверка
формы значения с внятным сообщением, и тест на `contract_version = "1.0"`.

**Тесты** (fake-адаптеры, как в существующем `tests/delivery/test_registry.py`): совместимый → в реестре;
minor больше ядра → reject; чужой major → reject; нет `contract_version` → reject; кривая форма версии →
reject с внятной причиной; `ep.load()` бросает → reject, соседние грузятся; `incompatible_adapters()`
содержит причины.

---

### Задача 3 — `contract_version` в vts-outline

`vts-outline/vts_outline/__init__.py`: объявить `contract_version = (1, 0)`.
Без этого после задачи 2 реальный адаптер перестанет грузиться.

**Тест:** в тестах vts-outline — адаптер проходит `isinstance(..., DeliveryAdapter)` и его версия
совместима с `CONTRACT_VERSION` ядра.

---

### Задача 4 — конфигурация loader'а в Settings

`vts/core/config.py`:
- `delivery_plugin_sources: list[...]` — элементы `{repo, token_env}`. В env — CSV/JSON по
  существующему паттерну `_CsvEnvSource`.
- `delivery_plugin_cache_dir: str = "/opt/vts/state/plugins"`.
- `delivery_plugin_contract_strict: bool = False`.

**Осторожно:** `_CsvEnvSource` умеет CSV → список СТРОК. Список объектов `{repo, token_env}` так не
задаётся. Решить явно: либо форма `repo|token_env` через разделитель с парсингом, либо в env только
JSON, а CSV — не поддерживать для этого поля. Не тащить сложность: проще yaml для списка + JSON в env.

**Тесты:** пустой список по умолчанию; yaml-оверрайд читается; env-форма парсится; имя token_env — это
ИМЯ переменной, сам токен в конфиг не попадает.

---

### Задача 5 — loader: скачивание и установка

Новый `vts/delivery/loader.py` + `python -m vts.delivery.loader`.

Структура — с изолированным сетевым слоем, чтобы тестировать без сети:
- `GitHubReleases` (тонкий клиент: latest release, assets, download) — мокается в тестах через `respx`.
- Чистые функции: сравнение digest с manifest, атомарная запись manifest.
- `main()` — оркестрация по потоку из спеки.

Ключевые требования (все из спеки, каждое = тест):
- пустые источники → ничего не делает, exit 0;
- совпавший digest → skip БЕЗ скачивания;
- новый digest → download → sha256-проверка → `pip install --target=site/ --no-deps` → запись в manifest;
- digest-mismatch → НЕ ставит и НЕ пишет manifest;
- недоступный источник → лог + продолжение остальных (деградация пер-источник), exit 0;
- `.lock` занят → ранний выход, exit 0;
- manifest пишется атомарно и только ПОСЛЕ успешного install.

**Осторожно 1:** exit 0 на сетевых сбоях — это НЕ «глотать всё». Внутренние ошибки (нет прав на
cache-dir, нечитаемый конфиг) должны давать ненулевой код. Разделить два класса ошибок явно.

**Осторожно 2:** `pip install --target` в тестах — только на локально собранном wheel, без сети
(`--no-index`). Реальный GitHub в CI не дёргаем (проектный принцип «реальный Outline не дёргаем»).

**Осторожно 3:** digest из GitHub API — открытый вопрос спеки. Целостность считаем сами (sha256 после
скачивания) в любом случае; если API не отдаёт digest, skip-логика опирается на то, что отдаёт (etag/
size+updated_at) — зафиксировать выбранное в коде комментарием.

---

### Задача 6 — bootstrap в топологии

- `docker/vts-entrypoint.sh`: роль `plugins` → `python -m vts.delivery.loader`.
- `deploy/vts.yaml`: второй initContainer ПОСЛЕ `migrate` (initContainers выполняются по порядку),
  монтирующий том host-cache.
- `PYTHONPATH` для webapi/worker должен включать `<cache>/site`.

**Осторожно:** том под host-cache должен быть общим для init-контейнера и обоих основных контейнеров,
иначе установленное просто не видно. Существующий `opt-vts` (hostPath `/opt/vts`) уже смонтирован во
все три — дефолт `/opt/vts/state/plugins` попадает внутрь него, отдельный том не нужен. Проверить, что
каталог создаётся с нужными правами.

---

### Задача 7 — правка спеки loader'а (по решению из vts-929)

В спеку дописать: `config` адаптера может приходить СМЁРЖЕННЫМ из нескольких источников (учётка +
таргет). Это решение vts-929; фиксируется здесь, чтобы наследование позже не стало сюрпризом для
авторов плагинов, пока контракт не опубликован.

---

## Границы

**Не делаем** (из спеки, отложено): `GET /api/delivery-adapters` и MCP `list_delivery_adapters`;
CI-триггер репо плагинов; сам репо плагинов; горячая перезагрузка.

**Не делаем** (из vts-929): расщепление учётки и таргета — отдельная задача, здесь только строчка
в спеке.

## Проверка по завершении

- Весь прогон `pytest` зелёный.
- Каждый новый регресс-тест проверен на то, что он ПАДАЕТ без соответствующей правки.
- Реальный `vts-outline` грузится через registry с валидацией.
- Ручная проверка loader'а на локальном wheel без сети.
