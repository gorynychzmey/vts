# vts

Production-ready self-hosted service for video transcription and summarization.

## Documentation map

- Production first-time setup: `docs/INITIAL_DEPLOYMENT.md`
- Spec compliance and key implementation points: `docs/SPEC_COMPLIANCE.md`
- Detailed processing contract audit (download/transcribe/summary): `docs/PROCESSING_CONTRACT.md`
- Workflow and release contract: `PROJECT_RULES.md`
- Runtime example config: `config.yaml`
- systemd runtime env template: `systemd/vts.env.example`

## Stack

- Python 3.14+
- FastAPI (`webapi`) + SSE + minimal SPA
- Async SQLAlchemy + Postgres + Alembic
- Redis queue + pub/sub events (`vts:` prefix)
- Worker pipeline (`yt-dlp`, `ffmpeg`, Whisper API, llama.cpp API)
- Podman containers + systemd units

## Runtime architecture

Containers:

1. `webapi` (`vts.api.main`)
2. `worker` (`vts.worker.main`)
3. Postgres
4. Redis
5. External Whisper ASR webservice
6. External llama.cpp server

Whisper and llama servers are external dependencies and are not implemented in this repository.

## Data flow

```mermaid
flowchart LR
  User([User / Browser])
  PWA[PWA / SPA]
  API[webapi<br/>FastAPI + SSE]
  PG[(Postgres)]
  R[(Redis<br/>queue + pub/sub)]
  W[worker]
  YT[yt-dlp + ffmpeg]
  ASR[Whisper ASR<br/>HTTP service]
  LLM[llama.cpp / Ollama<br/>HTTP server]
  FS[(artifacts_root<br/>/srv/vts-data)]

  User -- "Google OAuth → vts_session cookie" --> PWA
  PWA -- "POST /api/tasks (cookie)" --> API
  PWA <-- "GET /api/events (SSE)" --> API
  API -- "enqueue" --> R
  API <-- "task/step state" --> PG
  R -- "pop job" --> W
  W -- "publish events" --> R
  R -- "fan-out events" --> API
  W -- "step rows" --> PG
  W -- "download / segment" --> YT
  YT -- "media + WAV segments" --> FS
  W -- "POST audio" --> ASR
  ASR -- "segments_raw.json" --> FS
  W -- "POST /chat/completions, /tokenize, /detokenize, /props" --> LLM
  LLM -- "window/final notes" --> FS
```

The artifact tree (under `artifacts_root/<task_id>/`) is the durable boundary between stages: each step writes named files there, and the next step reads them. Postgres holds task/step status; Redis carries the queue and the SSE event stream.

## External model services

Install/deploy these separately:

- Whisper ASR webservice:
  - Image: `ghcr.io/ahmetoner/whisper-asr-webservice:latest`
  - Docs: `https://github.com/ahmetoner/whisper-asr-webservice`
- llama.cpp OpenAI-compatible server:
  - Image: `ghcr.io/ggerganov/llama.cpp:server`
  - Docs: `https://github.com/ggerganov/llama.cpp/tree/master/examples/server`
- Diarization sidecar (own image, `diar-build-X.Y.Z` tag): required for
  `diarize=true`. **Speaker registry matching additionally requires sidecar
  1.1.0+** — the version that serves `POST /embed` and reports
  `embedding_model` on its responses. Older sidecars omit both; matching
  degrades to "no signal" rather than erroring (`embedding_model` defaults
  to `""` when absent — see `DiarizationBackend.normalize_output` in
  [`vts/services/diarization/_base.py`](../vts/services/diarization/_base.py)),
  so a prod deploy of the speaker registry needs at least a
  `diar-build-1.1.0` sidecar release.

## Pipeline stages

The DAG is defined in [`vts/pipeline/types.py`](../vts/pipeline/types.py) as a fixed ordered list. Each stage is a method on `TaskProcessor` ([`vts/pipeline/processor.py`](../vts/pipeline/processor.py)); the runner walks the list, persists a row in `steps` for each stage, and resumes by replaying the list and skipping completed stages whose `dry_run=True` check confirms their on-disk artifacts are still present.

| # | Stage | Input | Output | Restart marker |
|---|-------|-------|--------|----------------|
| 1 | `download` | `source_url` or uploaded file | `media/video.mkv` (unless `audio_only`), `media/audio.original.<ext>` | Either media file or downstream segments exist |
| 2 | `extract_audio` | `media/audio.original.<ext>` | `media/audio.wav` (16kHz mono PCM) | `audio.wav` exists |
| 3 | `trim_initial_silence` | `media/audio.wav` | rewritten `media/audio.wav` + trim metadata | Marker file / step row |
| 4 | `segment_audio` | `media/audio.wav` | `segments/0001.wav … NNNN.wav` with `overlap_seconds` overlap | Segment files exist |
| 5 | `detect_language` | first segment | `language` column on `tasks` | Field populated |
| 6 | `transcribe_segments` | `segments/*.wav` (parallel: `transcribe_parallel_per_task`) | `asr_segments` rows + `asr/segments_raw.json` | Per-segment row present |
| 7 | `merge_transcript` | `asr_segments` rows | `outputs/transcript.txt`, `outputs/transcript.json` | Files exist |
| 8 | `prepare_llama_model` | LLM URL | warm `/props` cache, validated tokenizer | Cached props match |
| 9 | `prepare_summary_chunks` | `outputs/transcript.json` | `summary/chunks.json` (token-counted windows) | File exists |
| 10 | `summarize_windows` | chunks (parallel via gpu lane, llm priority) | `summary/window_NN.txt`, `summary/windows.json` | `windows.json` exists |
| 11 | `pack_window_notes` | `summary/windows.json` | `summary/packed_notes.json` (only when packing triggered) | File exists or skip-flag in step row |
| 12 | `summarize_final` | window notes (packed if applicable) | `summary/final.md`, `tasks.summary_path` | File exists |

Restart contract: `processor._maybe_skip_step()` calls the stage with `dry_run=True`; if it returns `True`, the step row is marked `skipped` and the next stage runs. Otherwise the stage executes from scratch — there is no partial-stage resume, just whole-stage idempotency.

## Design decisions

These are the non-obvious choices baked into the pipeline. Each links back to the implementation.

**Silence-aware segmentation with overlap.** `segment_audio` (in [`vts/services/media.py`](../vts/services/media.py)) runs ffmpeg `silencedetect` over the input, then for each target boundary at `k * segment_target_seconds` searches a `±segment_search_window_seconds` window for the closest silence and cuts there. Adjacent segments share `segment_overlap_seconds` of audio. Fixed-size chunks would cut mid-word and hurt ASR; pure silence-based segmentation would produce wildly variable lengths and complicate parallelism. The hybrid keeps segments within a predictable range while landing cuts on silence.

**Resource lanes.** The worker pool runs up to `worker_max_active_tasks` `process_task` coroutines concurrently in one process (default `4`; set `VTS_WORKER_MAX_ACTIVE_TASKS=1` for legacy sequential behaviour). A single in-process `LaneManager` (in [`vts/worker/lanes.py`](../vts/worker/lanes.py)) is shared across all active tasks and arbitrates three named lanes, each a simple slot scheduler:

- `network` — 1 slot by default (downloads).
- `ffmpeg` — 2 slots by default (segmentation/media processing).
- `gpu` — 1 slot by default (Whisper transcription and LLM calls, which share the same GPU/VRAM).

`network` and `ffmpeg` slots are acquired once per pipeline step in `_run_step`. The `gpu` lane is acquired at per-GPU-call granularity — around each individual GPU/VRAM-heavy operation inside a step method (the former `HeavySlot` call sites: `detect_language`, `transcribe` segment, LLM warmup, summarize window, pack batch, finalize) — rather than for the whole step, so a single step with several GPU calls yields the slot between them instead of holding it the entire time.

The `gpu` lane has two priority classes: `asr` (transcription-related calls) is served ahead of `llm` (summarization-related calls) whenever both have waiters, so transcription throughput doesn't stall behind long LLM generations. To prevent LLM starvation under sustained ASR load, an anti-starvation burst limit `gpu_asr_burst` (default `3`) caps how many consecutive `asr` grants can be made before an `llm` waiter is served.

Night mode (`night_mode_enabled`) gates only `gpu` lane grants — acquisitions on `network` and `ffmpeg` are unaffected, but a `gpu` acquire blocks until an allowed hour if night mode is on and the current hour falls outside `night_mode_start_hour`–`night_mode_end_hour`.

Unbounded GPU parallelism on a single-GPU host thrashes VRAM and slows everything down; the `gpu` lane's slot count is the explicit backpressure point, now decoupled from network/ffmpeg concurrency so downloads and segmentation for other tasks are never blocked behind a GPU-bound task.

A task that is partially processed and queued waiting on a lane for its next step is marked with task status `waiting` (distinct from `running`), so the API/UI can distinguish "actively executing a step" from "queued behind a lane slot."

**llama.cpp HTTP server as baseline API.** The LLM client in [`vts/services/summarizer.py`](../vts/services/summarizer.py) talks to four endpoints — `GET /props`, `POST /tokenize`, `POST /detokenize`, `POST /chat/completions`. Only the last is in the OpenAI standard. The other three are needed because token budgeting (Stage A/B/C adaptive ratios) requires an authoritative token count on the *server's* tokenizer, not a guess from a local tiktoken-style heuristic. OpenAI-compatible servers that lack `/tokenize` must supply a local tokenizer file via `llm_tokenizer_path` (see [LLM_BACKENDS.md](LLM_BACKENDS.md)).

**Sliding-window summarization with optional packing.** `summarize_windows` slides a `window_tokens` window (default ≈ 2000 tokens) over the transcript with 15% overlap, producing one note per window. A single-shot summary over the full transcript is impossible above the LLM's context size and unstable below it. Two stages also let each window's output be checked for quality (compression ratio, redundancy, number/date/unit mismatches) independently — see the metrics schema below. When the concatenated window notes plus the final prompt would exceed `summary_n_ctx - summary_safety_margin`, `pack_window_notes` (Stage B) batches and re-compresses them before `summarize_final` runs.

## Metrics (JSONL)

Every task run emits structured metrics to a JSONL file (one JSON object per line) and duplicates each event as a single log line.

**Config keys** (all have `VTS_` env prefix):

| Key | Default | Description |
|-----|---------|-------------|
| `metrics_enabled` | `true` | Enable/disable metrics collection |
| `metrics_jsonl_path` | `/opt/vts/logs/metrics.jsonl` | Path to the JSONL output file |
| `metrics_redundancy_shingle_n` | `3` | Word n-gram size for SimHash redundancy |
| `metrics_redundancy_simhash_bits` | `64` | SimHash bit width |
| `metrics_redundancy_max_hamming` | `3` | Max Hamming distance for near-duplicate detection |

**Event stages emitted per task:**

- `download`, `extract_audio`, `trim_initial_silence`, `segment_audio`, `detect_language`, `transcribe_segments`, `merge_transcript`, `prepare_llama_model`, `prepare_summary_chunks`, `summarize_windows`, `pack_window_notes`, `summarize_final` — wall time per pipeline step
- `transcribe.segment` — per-segment ASR: `rtf`, `t_wall_ms`, `t_queue_ms`, `retries`
- `summarize.segment` — per-window LLM: `llm_prompt_tokens`, `llm_completion_tokens`, `llm_tok_per_s`, `compression_ratio`, `redundancy_dup_sentence_ratio`, `number_mismatch_count`, `format`
- `summarize.global` — final summary: same fields as above + `packing_triggered`
- `task.final` — aggregates: `p50/p95` for RTF, tok/s, compression ratio, redundancy; worst-3 by number mismatch and redundancy

**Example JSONL line** (`summarize.segment`):

```json
{"ts":"2026-03-03T12:00:00.000Z","task_id":"abc","run_id":"xyz","stage":"summarize.segment","status":"ok","segment_id":1,"t_wall_ms":9800,"t_queue_ms":120,"llm_prompt_tokens":450,"llm_completion_tokens":180,"llm_total_tokens":630,"llm_tok_per_s":18.37,"llm_ctx_utilization":0.0137,"compression_ratio":0.4,"redundancy_dup_sentence_ratio":0.0,"numbers_in_summary":2,"numbers_in_transcript":3,"number_mismatch_count":0,"dates_in_summary":0,"dates_in_transcript":0,"date_mismatch_count":0,"units_in_summary":1,"units_in_transcript":2,"unit_mismatch_count":0,"format":{"paragraph_count":2,"bullet_ratio":0.0,"heading_count":0,"format_violations":[]},"prompt_version":""}
```

Read the log: `tail -f /opt/vts/logs/metrics.jsonl | python3 -m json.tool`

## yt-dlp YouTube auth and diagnostics

When YouTube returns `HTTP 403`, configure `yt-dlp` runtime options in `config.yaml` (or `VTS_*` overrides):

- `ytdlp_cookies_file` (`VTS_YTDLP_COOKIES_FILE`)
- `ytdlp_cookies_from_browser` (`VTS_YTDLP_COOKIES_FROM_BROWSER`, JSON array in order `[browser, profile, keyring, container]`)
- `ytdlp_youtube_player_client` (`VTS_YTDLP_YOUTUBE_PLAYER_CLIENT`)
- `ytdlp_youtube_po_token` (`VTS_YTDLP_YOUTUBE_PO_TOKEN`)
- `ytdlp_verbose` (`VTS_YTDLP_VERBOSE`)

Worker automatically remembers the last successful YouTube `player_client` per user in DB and reuses it on next tasks.
If saved client fails, worker retries fallback clients and updates stored preference.

## Data model

Schema is managed by Alembic; baseline is `alembic/versions/0001_initial.py`, subsequent migrations evolve it (e.g. `0002` adds `users.preferred_ytdlp_client`, `0004` adds `tasks.source_title`, `0005` adds `tasks.summary_progress`, `0006` drops `asr_words`, `0008` adds `push_subscriptions`, `0009` adds `api_tokens`).

| Table | Columns | Keys & indexes |
|-------|---------|----------------|
| `users` | `id UUID PK`, `username TEXT UNIQUE`, `created_at TIMESTAMPTZ`, `preferred_ytdlp_client TEXT?` | unique on `username` |
| `tasks` | `id UUID PK`, `user_id UUID FK→users`, `source_url TEXT`, `source_title TEXT?`, `status ENUM(queued, running, paused, completed, failed, canceled, archived)`, `options JSON`, `artifact_dir TEXT`, `transcript_path TEXT?`, `summary_path TEXT?`, `summary_progress JSON?`, `error_message TEXT?`, `created_at`, `updated_at` | FK CASCADE; indexes `(user_id, created_at)`, `(status, created_at)` |
| `steps` | `id UUID PK`, `task_id UUID FK→tasks`, `name TEXT(64)`, `status ENUM(pending, running, completed, failed, skipped)`, `attempt INT`, `started_at?`, `finished_at?`, `message TEXT?` | FK CASCADE; unique `(task_id, name)`; index `(task_id, status)` |
| `asr_segments` | `id UUID PK`, `task_id UUID FK→tasks`, `segment_index INT`, `start_sec FLOAT`, `end_sec FLOAT`, `text TEXT`, `raw_json JSON` (full Whisper response) | FK CASCADE; unique `(task_id, segment_index)`; index `(task_id, start_sec)` |
| `push_subscriptions` | `id UUID PK`, `user_id UUID FK→users`, `endpoint TEXT`, `p256dh TEXT`, `auth TEXT`, `user_agent TEXT?`, `created_at` | FK CASCADE; one row per Web Push subscription |
| `api_tokens` | `id UUID PK`, `user_id UUID FK→users`, `name TEXT`, `token_hash CHAR(64)` (SHA-256), `prefix TEXT`, `created_at`, `last_used_at?`, `revoked_at?` | FK CASCADE; unique on `token_hash`; index `(user_id, revoked_at)` |

The `tasks.artifact_dir` is the per-task subdirectory under `artifacts_root` that holds every on-disk artifact (see *Processing Artifacts* below). The `steps` table is the durable record of which DAG stages have completed; the restart contract reads it on worker startup.

## Auth and user context

Authentication is handled in-process by vts. All paths converge in
[`resolve_user_from_request`](../vts/services/auth.py), which picks one of
three branches:

1. **Browser / web UI** — Google OAuth 2.0 → signed `vts_session` cookie
   (Starlette `SessionMiddleware`) → server-side sid record in Redis with
   matching TTL. Logout (`POST /auth/logout`) deletes the Redis record;
   the cookie alone is not enough to authenticate.
2. **MCP clients** — same Google client; FastMCP-managed bearer tokens
   whose `email` claim is checked against the allow-list on every call.
3. **Dev mode** (`oauth_enabled=false`) — trust `X-Forwarded-User` header.
   No proxy IP gating; for local development only.

User identity is the lowercased email. Missing users are auto-created on
first successful auth (OAuth callback or dev-mode header). Data is
isolated by user id throughout the pipeline.

The allow-list (`oauth_allowed_domains` ∪ `oauth_allowed_emails`) is
**re-checked on every request** so removing someone takes effect on the
next call. Both empty → fail-safe deny.

State-changing `/auth/*` endpoints additionally require `Sec-Fetch-Site`
in `{same-origin, same-site, none}` (CSRF gate), see
[`vts/api/csrf.py`](../vts/api/csrf.py).

Admins (configured via `admin.emails` / `VTS_ADMIN_EMAILS`) can switch
context with `?as_user=<email>`; the target user must already exist
(admins do not auto-create by impersonation). Tasks created while
impersonating are owned by the target user.

For the full picture — flows, configuration matrix, session secret
persistence, MCP OAuth metadata location, security model — see
[docs/AUTH.md](AUTH.md).

## Configuration reference

Config is loaded from `/opt/vts/config/config.yaml` (or `./config.yaml` in dev) and overlaid with environment variables. The env name is `VTS_` + the Settings field name in [`vts/core/config.py`](../vts/core/config.py).

**YAML → env mapping.** Nested YAML keys are flattened by joining segments with underscores; the flat key is then matched (sometimes via an alias) to a `Settings` field. Examples:

- `services.llm.model` → `services_llm_model` → field `llm_model` → env `VTS_LLM_MODEL`
- `summary.segment.ratio` → `summary_segment_ratio` → env `VTS_SUMMARY_SEGMENT_RATIO`
- `ytdlp.youtube.player_client` → `ytdlp_youtube_player_client` → env `VTS_YTDLP_YOUTUBE_PLAYER_CLIENT`

Legacy `services.llama.*` keys still resolve to the same `llm_*` fields for backward compatibility.

### All keys

Defaults shown match [`vts/core/config.py`](../vts/core/config.py). Every key listed here accepts a `VTS_<UPPER>` override.

**Runtime / network:**

| YAML path | Env | Default |
|-----------|-----|---------|
| `environment.host` | `VTS_HOST` | `0.0.0.0` |
| `environment.port` | `VTS_PORT` | `8080` |
| `services.database.url` | `VTS_DATABASE_URL` | `postgresql+asyncpg://vts:vts@postgres:5432/vts` |
| `services.database.write_throttle.ms` | `VTS_SERVICES_DATABASE_WRITE_THROTTLE_MS` | `150` |
| `services.redis.url` | `VTS_REDIS_URL` | `redis://redis:6379/0` |
| `services.redis.prefix` | `VTS_REDIS_PREFIX` | `vts:` |
| `trusted_proxy.cidrs` | `VTS_TRUSTED_PROXY_CIDRS` | `["127.0.0.1/32", "::1/128", "10.0.0.0/8", "172.16.0.0/12"]` |
| `admin.emails` | `VTS_ADMIN_EMAILS` | `[]` |
| `mcp_enabled` | `VTS_MCP_ENABLED` | `true` |
| `mcp_path` | `VTS_MCP_PATH` | `/mcp` |
| `dirs.artifacts` | `VTS_ARTIFACTS_ROOT` | `/srv/vts-data` |
| `dirs.prompts` | `VTS_PROMPTS_DIR` | `/opt/vts/prompts` |
| `timezone` | `VTS_TIMEZONE` | `null` (use system) |
| `operator_name` | `VTS_OPERATOR_NAME` | `null` |
| `operator_contact` | `VTS_OPERATOR_CONTACT` | `null` |
| `operator_instance_name` | `VTS_OPERATOR_INSTANCE_NAME` | `null` |

The three `operator_*` keys, when set, are rendered on the public
`/privacy` page so users know who is running the instance they are
talking to. Leave them unset to keep the page neutral; the page will
say "ask whoever gave you the link" instead.

**External services:**

| YAML path | Env | Default |
|-----------|-----|---------|
| `services.whisper.url` | `VTS_WHISPER_URL` | `http://whisper:9000` |
| `services.whisper.backend` | `VTS_WHISPER_BACKEND` | `asr` (or `cpp`) |
| `services.llm.url` | `VTS_LLM_URL` | `http://llama:8000/v1` |
| `services.llm.api_key` | `VTS_LLM_API_KEY` | `null` |
| `services.llm.model` | `VTS_LLM_MODEL` | `Qwen2.5-7B-Instruct-Q4` |
| `services.llm.tokenizer_path` | `VTS_LLM_TOKENIZER_PATH` | `null` |
| `services.llm.temperature` | `VTS_LLM_TEMPERATURE` | `0.2` |
| `services.llm.top_p` | `VTS_LLM_TOP_P` | `null` |
| `services.llm.min_p` | `VTS_LLM_MIN_P` | `null` |
| `services.llm.repeat_penalty` | `VTS_LLM_REPEAT_PENALTY` | `null` |
| `services.llm.thinking` | `VTS_LLM_THINKING` | `null` |
| `services.llm.chat_timeout_seconds` | `VTS_LLM_CHAT_TIMEOUT_SECONDS` | `600` |
| `services.llm.final_timeout_seconds` | `VTS_LLM_FINAL_TIMEOUT_SECONDS` | `1800` |

**Authentication:** (see [docs/AUTH.md](AUTH.md) for semantics)

| YAML path | Env | Default |
|-----------|-----|---------|
| `oauth_enabled` | `VTS_OAUTH_ENABLED` | `false` |
| `oauth_client_id` | `VTS_OAUTH_CLIENT_ID` | `null` |
| `oauth_client_secret` | `VTS_OAUTH_CLIENT_SECRET` | `null` |
| `oauth_allowed_domains` | `VTS_OAUTH_ALLOWED_DOMAINS` | `[]` |
| `oauth_allowed_emails` | `VTS_OAUTH_ALLOWED_EMAILS` | `[]` |
| `public_base_url` | `VTS_PUBLIC_BASE_URL` | `null` |
| `session_secret` | `VTS_SESSION_SECRET` | `null` (autogenerated to file) |
| `session_secret_file` | `VTS_SESSION_SECRET_FILE` | `/opt/vts/state/session_secret` |
| `session_max_age_days` | `VTS_SESSION_MAX_AGE_DAYS` | `30` |

Legacy `mcp_oauth_*` env vars are still accepted as aliases of the
canonical `oauth_*` keys (deprecated; scheduled removal in 1.2.x).

**Pipeline tuning:**

| YAML path | Env | Default |
|-----------|-----|---------|
| `segment.target_seconds` | `VTS_SEGMENT_TARGET_SECONDS` | `300` |
| `segment.search_window_seconds` | `VTS_SEGMENT_SEARCH_WINDOW_SECONDS` | `30` |
| `segment.overlap_seconds` | `VTS_SEGMENT_OVERLAP_SECONDS` | `3` |
| `trim_silence.threshold_db` | `VTS_TRIM_SILENCE_THRESHOLD_DB` | `-35.0` |
| `trim_silence.min_duration_sec` | `VTS_TRIM_SILENCE_MIN_DURATION_SEC` | `0.4` |
| `trim_silence.max_seconds` | `VTS_TRIM_SILENCE_MAX_SECONDS` | `30.0` |
| `language_detection.confidence_threshold` | `VTS_LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD` | `0.60` |
| `transcribe.parallel_per_task` | `VTS_TRANSCRIBE_PARALLEL_PER_TASK` | `2` |
| `worker.max_active_tasks` | `VTS_WORKER_MAX_ACTIVE_TASKS` | `4` |
| `lane.network_slots` | `VTS_LANE_NETWORK_SLOTS` | `1` |
| `lane.ffmpeg_slots` | `VTS_LANE_FFMPEG_SLOTS` | `2` |
| `lane.gpu_slots` | `VTS_LANE_GPU_SLOTS` | `1` |
| `gpu.asr_burst` | `VTS_GPU_ASR_BURST` | `3` |
| `event_throttle.hz` | `VTS_EVENT_THROTTLE_HZ` | `4` |
| `task_cancel_ttl.seconds` | `VTS_TASK_CANCEL_TTL_SECONDS` | `3600` |
| `night_mode.enabled` | `VTS_NIGHT_MODE_ENABLED` | `false` |
| `night_mode.start_hour` | `VTS_NIGHT_MODE_START_HOUR` | `22` |
| `night_mode.end_hour` | `VTS_NIGHT_MODE_END_HOUR` | `7` |
| `media_ttl.hours` | `VTS_MEDIA_TTL_HOURS` | `72` |

**Speaker matching (voice registry):**

| YAML path | Env | Default |
|-----------|-----|---------|
| `services.speaker.match_max_distance_auto` | `VTS_SERVICES_SPEAKER_MATCH_MAX_DISTANCE_AUTO` | `0.25` |
| `services.speaker.match_max_distance_candidate` | `VTS_SERVICES_SPEAKER_MATCH_MAX_DISTANCE_CANDIDATE` | `0.55` |
| `services.speaker.preview_count` | `VTS_SERVICES_SPEAKER_PREVIEW_COUNT` | `3` |
| `services.speaker.preview_seconds` | `VTS_SERVICES_SPEAKER_PREVIEW_SECONDS` | `5.0` |
| `services.speaker.preview_min_segment` | `VTS_SERVICES_SPEAKER_PREVIEW_MIN_SEGMENT` | `2.0` |

A voice fragment is matched against known speakers by cosine distance
between embeddings (vectors are unnormalised, so cosine — not L2 — is the
only sane operator):

- distance `<= match_max_distance_auto` → auto-bind the fragment to that
  registry person, no user action needed.
- distance `> match_max_distance_candidate` → not even offered as a
  candidate; too far to be a plausible match.
- in between → grey zone: offered as a candidate for the user to confirm
  or reject during manual review, not auto-bound.

`preview_count`/`preview_seconds`/`preview_min_segment` control how the
short audio clips played back in the resolution dialog are cut: how many
clips per speaker, the target clip length, and the minimum source-segment
length eligible for cutting a clip from.

These thresholds are deliberately plain config, not constants, because
they are calibrated from real data and expected to move without a
rebuild. A live measurement against the reference meeting (2026-07-17)
confirmed the `0.25` auto threshold catches every true speaker match and
rejects every false one on that dataset — treat it as a starting
calibration, not a proof for all corpora; revisit if false
auto-binds/misses show up in production.

**Summarization (adaptive token budgeting):**

| YAML path | Env | Default |
|-----------|-----|---------|
| `summary.n_ctx` | `VTS_SUMMARY_N_CTX` | `32768` |
| `summary.safety_margin` | `VTS_SUMMARY_SAFETY_MARGIN` | `768` |
| `summary.segment.ratio` | `VTS_SUMMARY_SEGMENT_RATIO` | `0.40` |
| `summary.segment.min_ratio` | `VTS_SUMMARY_SEGMENT_MIN_RATIO` | `0.30` |
| `summary.segment.max_ratio` | `VTS_SUMMARY_SEGMENT_MAX_RATIO` | `0.55` |
| `summary.segment.min_floor` | `VTS_SUMMARY_SEGMENT_MIN_FLOOR` | `200` |
| `summary.segment.max_cap` | `VTS_SUMMARY_SEGMENT_MAX_CAP` | `1800` |
| `summary.pack.ratio` | `VTS_SUMMARY_PACK_RATIO` | `0.90` |
| `summary.pack.min_ratio` | `VTS_SUMMARY_PACK_MIN_RATIO` | `0.80` |
| `summary.pack.max_ratio` | `VTS_SUMMARY_PACK_MAX_RATIO` | `0.95` |
| `summary.pack.min_floor` | `VTS_SUMMARY_PACK_MIN_FLOOR` | `400` |
| `summary.pack.batch_max_input_tokens` | `VTS_SUMMARY_PACK_BATCH_MAX_INPUT_TOKENS` | `12000` |
| `summary.final.ratio` | `VTS_SUMMARY_FINAL_RATIO` | `0.70` |
| `summary.final.min_ratio` | `VTS_SUMMARY_FINAL_MIN_RATIO` | `0.60` |
| `summary.final.max_ratio` | `VTS_SUMMARY_FINAL_MAX_RATIO` | `0.80` |

**Web Push (VAPID):** `vapid_public_key`, `vapid_private_key`, `vapid_subject` — see the PWA section below.

**Metrics:** see the *Metrics (JSONL)* section above — `metrics.enabled`, `metrics.jsonl_path`, `metrics.redundancy.*`.

**yt-dlp:** see the *yt-dlp YouTube auth and diagnostics* section above.

## PWA: install, share target, push notifications

Installed on Android/desktop (Chromium), the app exposes a [PWA manifest](vts/static/manifest.webmanifest):

- **Share target**: once installed, vts appears in the system share sheet. Sharing a URL (e.g. from YouTube) opens the app with the URL pre-filled in the New Task form. Sharing a video/audio file loads it into the file input. The user still picks options and submits manually.
- **Web Push notifications**: the bell icon in the header asks for notification permission and subscribes the browser. When a task finishes (`completed` or `failed`), the server sends a push. Clicking the notification focuses the app and scrolls to the task.

To enable push, set the VAPID keys on the server:

- `vapid_public_key` (`VTS_VAPID_PUBLIC_KEY`) — base64url-encoded public key, also exposed to the frontend.
- `vapid_private_key` (`VTS_VAPID_PRIVATE_KEY`) — base64url-encoded private key.
- `vapid_subject` (`VTS_VAPID_SUBJECT`) — contact URL for the push service, e.g. `mailto:ops@example.com`.

Generate a keypair once and paste into config:

```
python scripts/generate_vapid_keys.py
```

If VAPID keys are not set, the bell icon stays hidden and push is disabled; the rest of the app works normally.

## Browser cache and auto-update

- `index.html` is served with `Cache-Control: no-store`.
- Frontend assets are versioned (`?v=<server_version>`).
- SPA polls `/api/version` and auto-reloads on version mismatch.

## Production vs local environment files

- Production uses `/opt/vts/config/config.yaml` as the source of truth.
- Production uses `/opt/vts/config/vts.env` mainly for image tag (`VTS_IMAGE`) and optional explicit overrides.
- `.env` / `.env.example` are for local `docker/podman compose` usage and are not required for systemd deployment.

## Build and image publish

`build.sh` builds and pushes one universal image to a container registry of
your choice (GHCR, Docker Hub, etc.):

- `<registry>/<owner>/vts:<version>`
- `<registry>/<owner>/vts:latest`

Example:

```bash
docker login ghcr.io
export CONTAINER_ENGINE=docker
export IMAGE_REPO=ghcr.io/OWNER/vts        # change OWNER to your GH user/org
export USE_BUILDX=auto
export BUILDX_CACHE_REPO=ghcr.io/OWNER/vts
export BUILDX_CACHE_MODE=max
export APT_MIRROR=http://deb.debian.org/debian
export APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
./build.sh
```

### Controlled GitHub Actions build

Workflow: `.github/workflows/build-images.yml`

Triggers:

- Manual run: `Actions -> Build Images -> Run workflow`
- Special push tag: `build-*` (for example `build-0.2.1`)
- Team convention: if request says `build` after commit/push, this means pushing `build-*` tag to trigger GitHub Actions build. Local `./build.sh` is run only on explicit request.
- Team convention (strict): `build` after commit/push always means the commit must be accompanied by pushed git tag `build-*`.
- Mandatory rule: before pushing `build-*`, bump version in `vts/__init__.py` and push that commit first.
- Mandatory rule: `build-*` tag version must match current project version.
- Mandatory rule: immediately after pushing `build-*`, start GitHub Actions monitoring in a background subagent.
- Mandatory rule: the subagent must watch the triggered workflow until final status and report the result back into the task.

Tag-trigger example:

```bash
git tag build-0.2.1
git push origin build-0.2.1
```

Notes:

- Build uses existing `build.sh` (including tests inside the built image before push).
- Version source:
  - for tag trigger `build-X.Y.Z`, workflow uses `X.Y.Z` as image version;
  - for manual run you can set input `build_version`;
  - fallback is `vts/__init__.py` version.
- Workflow pushes to both registries:
  - GHCR (primary push from GitHub Actions build)
  - Docker Hub (mirror from GHCR tags)
- Repository targets can be overridden by workflow inputs:
  - `dockerhub_image_repo`
  - `ghcr_image_repo`
- Or by repository variables:
  - `DOCKERHUB_IMAGE_REPO`
  - `GHCR_IMAGE_REPO`
- For Docker Hub pushes set repository secrets:
  - `DOCKERHUB_USERNAME`
  - `DOCKERHUB_TOKEN`
- GHCR push uses built-in `${{ secrets.GITHUB_TOKEN }}`.

### Auto deploy after successful build (optional)

Workflow: `.github/workflows/deploy-after-build.yml`

Trigger:

- Automatically runs after successful `Build Images` workflow.

Required repository secrets:

- `DEPLOY_HOST` (for example `vts.example.com`)
- `DEPLOY_SSH_KEY` (private key for deploy user)
- `DEPLOY_KNOWN_HOSTS` (exact known_hosts line for server key)

Optional repository variables (defaults shown):

- `DEPLOY_USER` (`root`)
- `DEPLOY_PORT` (`22`)
- `DEPLOY_REMOTE_DIR` (`/opt/vts`)
- `DEPLOY_ENV_FILE` (`/opt/vts/config/vts.env`)
- `WEBAPI_SERVICE` (`vts-webapi.service`)
- `WORKER_SERVICE` (`vts-worker.service`)

Prepare `DEPLOY_KNOWN_HOSTS` locally:

```bash
ssh-keyscan -H <your-hostname>
```

## Build performance notes

- Dockerfiles are multi-stage.
- BuildKit caches are used for `apt` and `pip wheel`.
- `build.sh` supports `docker buildx` registry cache (`cache-from` / `cache-to`).
- Image version label is applied at the end of runtime stage, so version bumps do not invalidate heavy `apt`/`pip` layers.
- Runtime images do not include test tooling.
- Local test tooling is in `requirements-dev.txt`.
- On Windows, fastest builds are typically from WSL2 with repository stored in Linux FS (`/home/<user>/...`), not under `C:\...`.

## API summary

**Machine-readable spec** (for GPT Custom Actions, curl, generated
client libs): `GET /openapi.json`. Interactive Swagger UI at `/docs`,
ReDoc at `/redoc`. Both advertise `securitySchemes.ApiToken` (HTTP
Bearer); browser/MCP-only routes (`/auth/*`, `/api/me/tokens`,
`/api/events`, `/api/push/*`, `/player/<id>`) are excluded. See
[docs/API.md](API.md) for setup notes.

REST endpoints:

- `POST /api/tasks`
- `GET /api/tasks`
- `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/pause`
- `POST /api/tasks/{id}/resume`
- `DELETE /api/tasks/{id}`
- `GET /api/tasks/{id}/transcript`
- `GET /api/tasks/{id}/summary`
- `GET /api/version`
- `GET /api/me`
- `GET /api/me/tokens` / `POST /api/me/tokens` / `DELETE /api/me/tokens/<id>` (session-only)
- `GET /api/admin/users` (admin only)

Auth endpoints (active when `oauth_enabled=true`):

- `GET  /auth/login`     — start OAuth flow
- `GET  /auth/callback`  — Google redirects here; sets `vts_session` cookie
- `POST /auth/logout`    — revoke server-side sid; CSRF-gated by `Sec-Fetch-Site`

MCP OAuth endpoints (mounted at host root per RFC 8414 / 9728):

- `GET /.well-known/oauth-authorization-server`
- `GET /.well-known/oauth-protected-resource`
- `/authorize`, `/token`, `/register`, `/consent`
- `GET /mcp/auth/callback` — FastMCP's Google redirect target (`mcp_path` configurable)

MCP transport itself: `POST /mcp/` (FastMCP streamable-http).

Auth: every request is resolved by
[`resolve_user_from_request`](../vts/services/auth.py). When
`oauth_enabled=true`, requests must carry either a `vts_session` cookie
(browser) or `Authorization: Bearer <token>` (MCP). When `oauth_enabled=false`,
the resolver falls back to the `X-Forwarded-User` header for dev use only.
User identity is the lowercased email; unknown users are auto-created on
first successful auth. Admins (`admin.emails` / `VTS_ADMIN_EMAILS`)
impersonate via `?as_user=<email>` (target must already exist); tasks
created while impersonating are owned by the target user. See
[docs/AUTH.md](AUTH.md) for flows, security model, and configuration.

### SSE: `GET /api/events`

Server-Sent Events stream of per-task updates, filtered to the requesting user. Each message arrives as `event: <name>\ndata: <json>\n\n`. The JSON envelope is `{user_id, task_id, event, data}` (see [`vts/services/redis_bus.py`](../vts/services/redis_bus.py)).

| `event` | When emitted | Notable `data` fields |
|---------|--------------|-----------------------|
| `server_version` | First message on connect | `version` |
| `ping` | Keep-alive (≈ every 30 s) | — |
| `task_status` | Task lifecycle transitions | `status` (queued/running/paused/completed/failed/canceled) |
| `step` | DAG stage start/end | `name`, `status`, optional `message` |
| `phase` | Sub-phases inside `download` / `extract_audio` | `phase`, `status` |
| `media_progress` | yt-dlp download progress (throttled at `event_throttle_hz`) | `downloaded`, `total`, `percent` |
| `segment_progress` | Audio segmentation progress | `segment_index`, `total` |
| `transcribe_progress` | Per-segment ASR completion | `segment_index`, `total` |
| `transcript_segment_text` | One segment of ready transcript text | `segment_index`, `text` |
| `llama_model_progress` | LLM warm-up (`prepare_llama_model`) | `phase`, `status` |
| `summary_progress` | Per-window summarization | `window_index`, `total` |
| `segment_summary_text` | One window summary as it lands | `window_index`, `text` |

All progress events that fire at high rates are throttled by `event_throttle_hz` (default 4 Hz) per task per channel.

## Task options

`POST /api/tasks` supports stage control options:

- `audio_only` (`false` by default): skip video stream download, keep only audio track.
- `transcript` (`true` by default): run transcription pipeline; if `false`, pipeline stops after download.
- `summary` (`true` by default): run summarization pipeline; requires `transcript=true`.
- `language` (`auto`/`ru`/`de`/`en` via UI; free string in API).
- `speaker_no_manual_stop` (`false` by default; requires `diarize=true`):
  skip the pipeline's manual-review pause for speaker resolution. Any
  fragment that didn't auto-bind to a registry person (see *Speaker
  matching* under Configuration reference) is left anonymous instead of
  blocking the task on user input.

Naming note:
- In the detailed contract, `do_transcribe` = `transcript`, `do_summary` = `summary`.
- API accepts both naming styles (`transcript/summary` and `do_transcribe/do_summary`).
- For full behavioral matrix and current gaps vs detailed contract see `docs/PROCESSING_CONTRACT.md`.

## Processing Artifacts

- Download artifacts:
  - `media/video.mkv` (when `audio_only=false`)
  - `media/audio.original.<ext>`
- Segmentation artifacts:
  - `segments/0001.wav`, `segments/0002.wav`, ...
- Transcription artifacts:
  - `asr/segments_raw.json`
  - `outputs/transcript.txt`
- Summary artifacts:
  - `summary/window_01.txt`, `summary/window_02.txt`, ...
  - `summary/windows.json` (per-window notes index)
  - `summary/packed_notes.json` (deduped/packed notes; present when Stage B packing was triggered)
  - `summary/final.md`

## Workflow summary

- Commit flow and semver rules: `PROJECT_RULES.md`
- Deployment flow and server bootstrap: `docs/INITIAL_DEPLOYMENT.md`
- Spec compliance audit and key code entry points: `docs/SPEC_COMPLIANCE.md`
- Detailed processing contract coverage and gap matrix: `docs/PROCESSING_CONTRACT.md`
- Helper scripts:
  - `scripts/bump_version.py`
  - `scripts/prepare_commit.sh` (cleans pytest temp caches, bumps patch, runs tests, stages changes)
  - `build.sh`
  - `deploy.sh`
