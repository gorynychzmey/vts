# Spec Compliance And Implementation Notes

Audit date: 2026-02-27

This document maps the repository implementation to the original VTS specification and records the most important implementation details for maintenance.

Status legend:
- `PASS`: implemented and present in repository
- `PARTIAL`: implemented with an important caveat
- `EXTENDED`: implemented and intentionally extended beyond baseline spec

## Compliance Matrix

| Spec area | Status | Notes |
| --- | --- | --- |
| Repository completeness (code, Dockerfiles, Alembic, scripts, prompts, env examples) | PASS | Present: `docker/`, `alembic/`, `build.sh`, `deploy.sh`, `PROJECT_RULES.md`, `config.yaml`, `prompts/*.md`, `requirements.txt`, `.env.example`. |
| Git workflow contract (checks + commit + push) | PASS | Documented in `PROJECT_RULES.md`; automated helper: `scripts/prepare_commit.sh`. |
| Semantic versioning rules and storage | PASS | `vts/__init__.py`, `/api/version`, Docker label `org.opencontainers.image.version`, bump automation in `scripts/bump_version.py`. |
| Deploy workflow (manual, bump minor, commit, push, build/push images, SSH pull, systemd restart) | PASS | Implemented in `deploy.sh`; build/push flow in `build.sh`; unit templates in `systemd/`. |
| Architecture (webapi + worker + Postgres + Redis + external Whisper + external llama.cpp) | PASS | Internal services implemented; external Whisper/llama explicitly not implemented in repo. |
| Auth model (`X-Forwarded-User` only from trusted proxy, auto-create user, per-user isolation) | PASS | Enforced in `vts/services/auth.py`; per-user task filtering in API/Repo methods. |
| Postgres async SQLAlchemy + Alembic + required tables + indexes | PASS | Implemented in `vts/db/models.py` and `alembic/versions/0001_initial.py`. |
| WAL enabled | PASS | `docker-compose.yml` starts Postgres with `-c wal_level=replica`. |
| Redis prefix/queue/pubsub + event throttle 4/sec | PASS | Prefix default `vts:` (`vts/core/config.py`), queue/pubsub and throttle in `vts/services/redis_bus.py`. |
| Storage layout `/srv/vts-data/{user_hash}/{task_id}` and `/opt/vts/*` config/prompts | PASS | `vts/services/storage.py` and defaults in `vts/core/config.py`, `config.yaml`. |
| Detailed processing contract (download/transcribe/summarize) | PASS | Full section-by-section audit is in `docs/PROCESSING_CONTRACT.md`. |
| DAG steps and pipeline behavior | EXTENDED | Baseline 7 steps implemented plus extra warm-up step `prepare_llama_model` before summarization. |
| Step idempotency, output checks, DB status updates, SSE events, task logs | PASS | Implemented in `vts/pipeline/processor.py`; logs at `logs/task.log`. |
| Crash resume behavior | PASS | Worker startup requeues stale `running` tasks to `queued`, restores queued backlog into Redis, and resumes processing automatically. |
| Download via yt-dlp API with progress | PASS | Hook-based video/audio progress, explicit merge/postprocess phases, artifacts `video.mkv` and `audio.original.<ext>`. |
| Segmentation config (300s target, +/-30s search, silence detect, 3s overlap, wav 16k mono) | PASS | Implemented with silence detect `-30dB:d=1.0` and `segments/0001.wav` naming. |
| Transcription (Whisper call, raw + normalized segments + words, limits) | PASS | Implements language + initial_prompt, DB persistence, and raw artifact `asr/segments_raw.json`. |
| Merge overlap removal by timestamps | PASS | Strict cutoff: words with `t_start < previous_segment_end` are dropped. |
| Summary via llama.cpp endpoint, prompts from files, structured output, 2000/15% windows | PASS | Token-window strategy with window artifacts `summary/window_XX.txt` and final markdown `summary/final.md`. |
| Limits (resource lanes, optional night mode window) | PASS | `vts/worker/lanes.py`, config keys in `vts/core/config.py`. |
| Required API endpoints | PASS | All required endpoints are present in `vts/api/main.py`. |
| Task launch options (`audio only`, `transcript`, `summary`) | PASS | API and UI support stage control: audio-only download, transcript-only flow, and optional summary stage. |
| Minimal SPA requirements (URL input, checkboxes, language, task list, tabs, dual progress bars) | PASS | Implemented in `vts/static/index.html` + `vts/static/app.js`. |
| Cleanup policy (delete segment WAV, media TTL, keep transcript/summary) | PASS | Segment deletion, media TTL cleanup, transcript/summary retention, and raw ASR artifact retention are implemented. |
| Non-functional requirements (async webapi, type hints, structured logging, throttled DB writes) | PASS | Async FastAPI handlers, typed codebase, JSON logging (`vts/core/logging.py`), DB write throttling in pipeline. |

## Most Important Implementation Parts

| Area | Primary files | Why it matters |
| --- | --- | --- |
| API surface, SSE, auth wiring | `vts/api/main.py`, `vts/api/deps.py` | Defines all client-facing behavior, user isolation, and streaming events. |
| Auth and trusted proxy policy | `vts/services/auth.py`, `vts/core/config.py` | Security boundary for `X-Forwarded-User`; admin impersonation policy. |
| Pipeline orchestration (idempotency + retries/resume semantics) | `vts/pipeline/processor.py`, `vts/pipeline/types.py` | Core business flow from download to final summary with DB/SSE/log integration. |
| Concurrency controls | `vts/worker/lanes.py`, pipeline transcription step | Enforces per-lane slot limits (network/ffmpeg/gpu) with gpu asr/llm priority, plus optional night-window throttling on the gpu lane. |
| Media/ASR processing | `vts/services/media.py`, `vts/services/transcription.py`, `vts/db/repo.py` | Segment generation, Whisper integration, and normalized persistence model. |
| Data model and migrations | `vts/db/models.py`, `alembic/versions/0001_initial.py` | Contract for task state, DAG step state, and transcript data model. |
| Deployment and release automation | `scripts/bump_version.py`, `scripts/prepare_commit.sh`, `build.sh`, `deploy.sh`, `systemd/*.service` | Implements project workflow contract and production rollout sequence. |
| Operational docs | `README.md`, `PROJECT_RULES.md`, `docs/INITIAL_DEPLOYMENT.md` | Source of truth for developers and operators. |

## Operational Notes

- Current package version is `0.2.1` in `vts/__init__.py`.
- Local host test run may fail without Python/pytest toolchain; containerized test run is supported and was used for verification.
