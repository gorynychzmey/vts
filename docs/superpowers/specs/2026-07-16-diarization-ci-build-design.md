# CI-сборка образа диаризации (vts-tkq)

**Дата:** 2026-07-16
**bd:** vts-tkq
**Тип:** инфраструктура (CI/CD)
**Связано:** vts-5xz (диаризация), vts-ej4 (CPU-образ, сделана — образ 2.1 ГБ)

## Цель

Собирать образ диаризации (`docker/diarization/`) в CI, а не вручную. По образцу
сборки образа vts, но **полностью независимо** от неё — у образа диаризации свой
темп изменений (веса вшиты по sha256, `server.py` меняется редко).

## Ключевое решение: полная развязка от vts

Образ диаризации отвязан от релизного цикла vts. Причина: он меняется на
порядок реже. Пересобирать и пушить 2.1 ГБ на каждый релиз vts — впустую жечь
лимитированные CI-минуты ради неизменившегося образа.

Развязка сделана через **отдельный workflow** с **осознанным тег-триггером**
(не авто-по-paths — сборка всегда намеренная, симметрично vts).

**Что НЕ трогаем** (вся vts-сборка остаётся как есть):
- Скилл `/build` (`.claude/commands/build.md`) — тегает `build-X.Y.Z`, мониторит.
  Он не знает, ЧТО собирается; логика в build.sh/CI. Диаризация уезжает ниже
  скилла, он нетронут.
- `build.sh`, `docker/vts.Dockerfile`, `build-images.yml`.

**Новые файлы:**
- `.github/workflows/build-diarization.yml`
- `.github/workflows/deploy-after-diarization.yml`
- `build-diarization.sh`
- `docker/diarization/VERSION`

## Версионирование

Свой семвер, НЕ связанный с версией vts. Источник — `docker/diarization/VERSION`
(одна строка `X.Y.Z`; проще `__init__.py`, у образа нет Python-пакета).
Старт: `1.0.0` (образ уже собран и работает).

Тег релиза: `diar-build-X.Y.Z` (симметрично `build-X.Y.Z` у vts). Версия берётся
из тега (`${GITHUB_REF_NAME#diar-build-}`), с фолбэком на `VERSION` при
`workflow_dispatch`.

## Триггеры (build-diarization.yml)

```yaml
on:
  push:
    tags: ['diar-build-*.*.*']   # осознанный релиз, как build-* у vts
  workflow_dispatch:              # ручной запуск
```

Только тег + ручной запуск. `paths`-автотриггер СОЗНАТЕЛЬНО отвергнут: сборка
всегда намеренная (решение Виктора).

## build-diarization.sh

По образцу `build.sh`, но проще (нет тестового набора внутри контейнера).

```
VERSION из diar-build-X.Y.Z или docker/diarization/VERSION
IMAGE_REPO = ghcr.io/<owner>/vts-diarization  (отдельный repo от vts)
build -f docker/diarization/Dockerfile -t :X.Y.Z -t :latest
smoke-тест (см. ниже) — если падает, НЕ пушим
push :X.Y.Z и :latest в GHCR
```

Свой buildx cache-тег: `buildcache-diarization` (отдельный от `buildcache-vts`,
иначе кеши двух образов перетирают друг друга).

## Smoke-тест (в build-diarization.sh, до push)

У образа нет pytest-набора. Аналог `run_tests_in_container` у vts — smoke-тест,
ловящий ровно те поломки, что всплывали вручную (torchcodec не грузится, форма
DiarizeOutput):

```
docker run -d --network none <image>     # offline С САМОГО НАЧАЛА
ждём /health (до ~30с — модель грузится лениво)
генерируем крошечный синтетический WAV (пара тонов)
POST /diarize -> проверяем:
  - HTTP 200
  - ключи ответа == {segments, embeddings, num_speakers}
  - num_speakers >= 1
падение любой проверки -> set -euo pipefail роняет скрипт -> push не происходит
```

`--network none` с самого начала = smoke-тест ОДНОВРЕМЕННО проверяет
offline-инвариант (веса вшиты, рантайм не ходит в HF). Бесплатно.

Синтетический WAV проверяет КОНТРАКТ, не качество: число спикеров на
искусственных тонах непредсказуемо, поэтому `>= 1`, а не `== N`.

## Docker Hub mirror

Симметрично vts: после сборки+push в GHCR — зеркалить в Docker Hub
(`docker tag GHCR->DockerHub; push`). Опционально (пустой `DOCKERHUB_IMAGE_REPO`
отключает шаг, как у vts). Иначе у диаризации не было бы резервного зеркала,
которое есть у vts.

Репозиторий Docker Hub: `docker.io/<owner>/vts-diarization`.

## docker-compose.yml

Сейчас: `diarization.build: ./docker/diarization` (локальная сборка).
Меняем на `build:` + `image:` одновременно:

Добавляется одна строка `image:` к существующему блоку (не трогая уже
присутствующие `profiles`, `environment` с `DIAR_MIN_DURATION_OFF`, `restart`):

```yaml
  diarization:
    build: ./docker/diarization
    image: ${DIARIZATION_IMAGE:-ghcr.io/<owner>/vts-diarization:latest}
    profiles: ["diarize"]
    environment:
      DIAR_MIN_DURATION_OFF: ${DIAR_MIN_DURATION_OFF:-0.5}
    restart: unless-stopped
```

`docker compose build` работает локально (разработка), на проде тянется готовый
`image:` — как whisper/llama берутся готовыми образами. Версия через
`${DIARIZATION_IMAGE}` env, как vts через `${VTS_VERSION}`. Порядок Compose:
при наличии обоих `build:` и `image:` — `compose build` собирает и тегает как
`image:`, `compose up` без сборки тянет `image:` из registry.

## Тестирование

- **build-diarization.sh локально:** прогон на dev-машине (`CONTAINER_ENGINE=podman`),
  образ собирается, smoke-тест проходит, push пропустить (`--dry-run`-подобно или
  без креды GHCR — падение на push ожидаемо и не считается провалом smoke).
- **Smoke-тест сам по себе** уже покрывает: health, контракт /diarize, offline.
- CI-прогон workflow — по факту первого тега `diar-build-1.0.0`.

## Автодеплой (deploy-after-diarization.yml)

Симметрично vts (решение Виктора): `workflow_run` после успешной сборки
диаризации → SSH на прод-хост → `podman pull` нового образа → `systemctl
restart` сервиса диаризации. По образцу `deploy-after-build.yml`.

Отличия от vts-деплоя:
- `workflow_run.workflows: ['Build Diarization Image']` (не 'Build Images').
- `concurrency.group: deploy-after-diarization` (свой, чтобы не гонки с
  vts-деплоем).
- Перезапускает ОДИН сервис — диаризацию. Новая var `DIARIZATION_SERVICE`
  (дефолт `vts-diarization.service`), вместо `WEBAPI_SERVICE`/`WORKER_SERVICE`.
- На хосте читает `DIARIZATION_IMAGE` из env-файла (вместо `VTS_IMAGE`),
  `podman pull`, рестарт, `systemctl status` для fail-fast.

Переиспользует те же secrets/vars vts-деплоя: `DEPLOY_HOST`, `DEPLOY_SSH_KEY`,
`DEPLOY_KNOWN_HOSTS`, `DEPLOY_USER`/`PORT`/`REMOTE_DIR`/`ENV_FILE`,
`DEPLOY_JUMP_HOST`. Ничего нового заводить не нужно, кроме `DIARIZATION_SERVICE`
и `DIARIZATION_IMAGE` в env-файле прода.

Remote-скрипт — heredoc с `set -euo pipefail`, как у vts (логика в CI YAML, не
на хосте, версионируется).

## Открытые вопросы (в имплементацию, не блокируют)

- Точная форма `on:` (tag-only push + workflow_dispatch) — деталь синтаксиса YAML.
- Имя workflow для `workflow_run` в deploy-триггере должно ТОЧНО совпадать с
  `name:` в build-diarization.yml — при имплементации свериться дословно.

## Вне скоупа

- Ускорение инференса (vts-zrz), параллелизм (vts-bv7) — отдельные задачи.
- ONNX/квантизация образа — не входит; собираем текущий рабочий образ.
