# Task = processing runtime, Recording = result — design

Status: draft for review · bd: `vts-lzad` · blocks: `vts-qwcj` (tags, Linear VOS-86 + VOS-146), `vts-y1g8` (re-run with changed settings)

## Intent

Today a `Task` is both the processing job and the owner of what it produced;
a `Recording` (vts-8w1r) is a library entry created at the end, holding
*copies* of the task's paths, language and prompt results. The two objects
half-duplicate each other, and the duplication already leaks:

- archiving a task deletes `transcript.json` (player, subtitles, re-indexing
  source), the processed transcript and every custom prompt result, while the
  recording in the library keeps pointing at them;
- a task cloned from a donor (`_clone_from_donor`) never gets a recording, so
  it is missing from the library and from corpus search;
- prompt results live in `Task.options` and are snapshotted into
  `Recording.meta`, so a summary restart leaves the recording stale until the
  next upsert.

The model we want gives each object one role:

- **Task** is the runtime: how a source is processed. It owns the media, the
  log, step state and intermediate files (ASR segments, diarization output,
  summary windows, probes).
- **Recording** is the result. It is created when the task is created and is
  filled while the task runs — raw transcript first, then the processed one,
  the summary, prompt results, speaker naming. The task keeps showing those
  texts, but as the recording's objects, not its own.

The immediate reason is the tags feature (`vts-qwcj`): with this split, tags
belong to the recording only, and the task shows its recording's tags. The
lasting reason is a cleaner model that later features (re-run with changed
settings, `vts-y1g8`) can build on.

## Goals

- A recording exists for every task from the moment the task is created
  (all creation paths: URL form, file/multi-file upload, MCP/WebMCP
  `submit_video`, donor clone).
- The recording is the single owner of content results: transcript paths,
  processed transcript, summary, prompt results, language, duration, speaker
  naming decisions.
- Every reader of content (HTTP, UI, MCP, delivery, indexing) reads it from
  the recording.
- Archiving removes only task runtime data; everything the recording owns
  survives.
- Each stage below ships as its own release and leaves prod consistent.

## Non-goals

- Several tasks per recording. The link stays **1:1** (decided: with two tasks
  it is undefined whose prompt results the recording holds). Re-processing a
  recording is `vts-y1g8`, a separate feature on top of this one.
- Moving files. Task and recording already share one `artifact_dir`
  (`repo.py:542`); only DB ownership and addressing change.
- Changing what "delete task" does. It still deletes the task together with
  its recording and the directory (`delete_task_with_recording`, vts-t4kg —
  a deleted transcript must not survive in `transcript_chunks`).
- Tags. They come after this, in `vts-qwcj`.

## Anti-goals (would make this a failure even if it works)

- A library that shows half-made or empty entries as if they were finished.
- Content readable through one address and stale or missing through another
  during the transition.
- Breaking existing public addresses: the repo is public, `docs/API.md`
  documents `/api/tasks/{id}/...`, external MCP clients call
  `get_transcript(task_id)`, and VOS-134 deep links have the form
  `/player/{task_id}?t=…`.
- A stage that cannot be rolled back without losing data.

## Decisions

### Ownership split

| Item | Owner after the refactor | Today |
|---|---|---|
| media, `audio_16k*.wav`, probes, `segments_manifest.json`, `language_detection.json` | task | task |
| `asr/segments_raw.json`, `asr_segments` table | task (intermediate) | task |
| `outputs/diarization.json`, summary windows/chunks/notes, `*_prompt.md` | task | task |
| `logs/task.log`, steps, `summary_progress`, `awaiting_step` | task | task |
| `options` as the *request* (language asked, diarize, prompts, delivery, source files) | task | task |
| raw transcript (`outputs/transcript.txt`, `transcript.json`) | recording (`transcript_path`) | task column, copied |
| processed transcript (`outputs/redacted_transcript.txt`) | recording (new `redacted_path`) | derived from `artifact_dir` |
| summary (`summary/final.*`) | recording (`summary_path`) | task column, copied |
| prompt results (`summary/results/*`) | recording (new `prompt_results` column) | `Task.options.prompt_results`, snapshot in `Recording.meta` |
| detected language | recording (`language`) | `Task.options.detected_language`, copied |
| duration | recording (`duration_sec`) | copied from `asr_segments` |
| speaker naming (`match_decisions`), voice samples' origin (`voice_samples`) | recording (`recording_id` FK) | `source_task_id` FK |
| `outputs/speaker_matches.json`, `speaker_previews.json`, preview wavs | task (inputs of the naming dialog, see below) | task |
| delivery attempts | **task** (see below) | task |

**Delivery stays on the task.** A delivery attempt is an event of processing —
fired when the task finishes, retried per task, shown in the task card. Only
the *payload* changes: `resolve_variant` reads content from the task's
recording. (This revises the stage list as first discussed, where delivery
was listed among the FKs to move.)

**Speaker naming freezes at archive** (Victor). Changing who is who means
rebuilding the transcript, then the summary and prompts — runtime work, and an
archived task no longer has a runtime. So the naming dialog's inputs
(`diarization.json`, `speaker_matches.json`, previews) stay task runtime and
archiving deletes them; the *outcome* of naming (`match_decisions`, the
re-rendered transcript) belongs to the recording and survives. The
recording-side naming endpoints keep today's gate: no resolve once the task is
archived or gone (`can_resolve_speakers_task`).

**The 1:1 link stays where it is:** `recordings.source_task_id`, with its
partial unique index. It already exists, all
recording-side code uses it, and with 1:1 the direction is a matter of taste.
Change its `ON DELETE` only if a stage needs it (it does not: deletion goes
through `delete_task_with_recording`).

### Library visibility

A recording appears in the library once it has its first text (raw
transcript, i.e. `transcript_path` set). Without text it is hidden and
reachable only through its task — which is never lost from view: archived
tasks stay in the task list, and removing a task means deleting it, which
deletes the recording too.

**Completeness is the recording's own state, not derived from the task**
(Victor). A task status cannot carry it: `archived` is reached from
`completed`, `failed` and `awaiting_input` alike (`ARCHIVABLE_STATUSES`), so an
archived failure would look finished, and a recording whose task is gone would
have nothing to derive from. The recording gets a state column, set by the
pipeline and never touched by archiving:

| Recording state | Set when | Library mark |
|---|---|---|
| `processing` | task created / resumed / restarted | "processing" |
| `complete` | task completes | none |
| `incomplete` | task fails or is canceled | "incomplete" — nothing more will come |

Re-runs (summary restart today, `vts-y1g8` later) put the recording back to
`processing`. Backfill: recordings of completed tasks → `complete`; of failed
or canceled tasks → `incomplete`; of archived tasks → `complete` if the
summary (or, for transcript-only tasks, the transcript) exists, otherwise
`incomplete` — the pre-archive status is not stored anywhere.
Corpus search is unaffected (chunks exist only once a transcript exists).

### Old addresses stay

Task-addressed endpoints and MCP tools remain as thin adapters: resolve the
task's recording, then serve exactly what the recording endpoint serves.
`/player/{task_id}` keeps working (deep links from VOS-134). Task-only
runtime endpoints (`/log`, `/media`, status, steps) are unchanged.

## Approaches considered

- **A. One release.** One migration moves fields, FKs and data; every writer
  and reader is rewritten in the same branch. No transitional double-writing,
  but a huge long-lived branch next to other agents' work on `main`, and a
  migration plus code in one tag — a prod failure means rolling everything
  back, data included.
- **B. Staged, each stage its own release.** *(Chosen by Victor.)* Small
  steps, each verified on prod before the next; both current defects closed
  along the way. Costs more total time and temporary double-writing in stages
  1–3.
- **C. Facade only.** Keep storage, make recording endpoints read through the
  task. Rejected: looks clean from outside, but the task stays the owner — the
  opposite of the goal, and tags would still hinge on the task.

## Stages

Each stage is a release (`build-X.Y.Z`), with its migration (if any) shipped
in the image as usual.

### Stage 0 — library filter first *(builder-check call)*

Ship the "hide recordings without `transcript_path`" filter, the recording
state column (with its backfill and the pipeline setting it on
complete/fail/cancel/restart) and its mark in `RecordingOut` and the library,
on their own, before stage 1. Otherwise rolling
stage 1 back would run code without the filter over recordings created at
task start, and the library would fill with empty entries (anti-goal #1).

### Stage 1 — the recording exists from the start and receives the writes

- Create the recording in the same transaction as the task, in every
  creation path (`POST /api/tasks`, upload finalize incl. multi-file, MCP
  `submit_video`, donor clone). Today it appears at `merge_transcript` via
  `reindex_task` only when embeddings are enabled, otherwise at completion.
- Add `Recording.redacted_path` and `Recording.prompt_results` (JSON,
  reassigned never mutated — see the JSON-column rule).
- Pipeline writers (`persist_detected_language`, `transcribe_segments`,
  `merge_transcript`, `summarize_windows`, finalize / `persist_prompt_result`,
  rerender, summary restarts, donor clone) write to the recording. The old
  task fields are **still written too** (dual-write), so stage-0 readers keep
  working and the release can be rolled back.
- Donor clone *(builder-check call)*: the donor is another user's task
  (`find_completed_donor(exclude_user_id=…)`). The clone's recording gets the
  prompt results the clone requested whose files were copied; the donor's
  speaker naming (`match_decisions`) is **not** carried over — the voice
  registry is per user.
- Backfill: create recordings for tasks that have none (donor clones, tasks
  predating vts-8w1r if any), fill `redacted_path` and `prompt_results` from
  the task **only where the file still exists** — archived tasks have already
  lost them. File checks mean the backfill runs inside the container as a
  one-off script, not as a pure SQL migration. Readers must treat a missing
  file as "not available" (404 / absent tab), never a 500.

### Stage 2 — speaker naming belongs to the recording

- `match_decisions` and `voice_samples` get `recording_id`; migration fills
  it through `recordings.source_task_id`. Keep `source_task_id` readable
  until stage 4.
- Repo speaker methods (`speaker_names_for_task(s)`, `decisions_for_task`,
  `noise_labels_from_decisions`, `has_decisions_for_task`,
  `diarized_task_ids`, `tasks_featuring_speaker`, …) get recording-keyed
  equivalents; the person/diarized filters in corpus search and
  `list_recordings` stop hopping through `source_task_id`.
- Speaker resolution writes `recording_id`.

### Stage 3 — readers move to the recording

- Recording endpoints get the full content set: transcript, processed,
  summary, subtitles, transcript entries, prompt results, speaker
  matches/previews/resolve (gated on a live, non-archived task, as above),
  summary restart; a web player addressed by recording.
- Web UI (`app.js`, ~12 call sites) and MCP content tools switch to them;
  task-addressed endpoints and tools become the adapters described above.
- Delivery's `resolve_variant` reads from the recording.
- `TaskOut` content fields (`transcript_path`, `summary_path`,
  `redacted_path`, `prompt_results`) are served from the recording.

### Stage 4 — cleanup

- Archiving deletes only task runtime data (the ownership table above);
  recording-owned files are kept.
- Remove dual-writes; drop `Task.transcript_path`, `Task.summary_path`,
  `options.prompt_results` / `detected_language` reads,
  `Recording.meta.prompt_results`, `match_decisions.source_task_id` /
  `voice_samples.source_task_id` once nothing reads them.

## Constraints

- Migrations run on prod Postgres (separate host from the app); test on the
  same backend as prod (Postgres in CI and locally, never SQLite).
- Each stage must keep the tree releasable; other agents work on `main`
  concurrently.
- JSON columns must be reassigned, not mutated in place.
- `artifacts_root` is a container path: any file-touching backfill runs inside
  the container.
- Public-repo rule: no private infra details in code, docs or tests.

## Left to the builder

- Exact column names and migration split within a stage.
- Whether the web player by recording is a new route or the existing route
  taking either id.
- How the processing mark looks in the library (reuse existing status chips).
- Test layout; each stage needs tests that fail with its change reverted.
