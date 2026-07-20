# Speaker "noise" flag — design (vts-552)

## Problem

`drop_marginal_speakers` silently and irreversibly folds any diarized speaker
below `min_share` (5%) of speech into the dominant speaker, to suppress phantom
speakers pyannote invents on music/echo/noise. Two defects:

1. **It measures share from merged ASR-entry durations, not diarization time.**
   On task `9c096ee1` a real participant (SPEAKER_03) spoke 149s = **13.1% of
   diarized time**, but their turns are spread across many short interjections
   whose ASR-entry spans sum to only 28.7s = **3.0%**. Below the 5% floor, all
   four of SPEAKER_03's turns folded into the dominant SPEAKER_02. A genuine
   speaker was erased. (bug vts-0ws)

2. **The fold is irreversible and on autopilot** — over-merge (two people → one
   label) is the worst error class: their speech is physically concatenated and
   can only be bound to one person. Over-split (one person → two labels) is
   cheap: the operator binds both to the same person and the collapsed
   transcript is correct. The current rule optimizes against this asymmetry.

## Solution overview

Replace the irreversible auto-fold with a reversible **per-speaker "noise" flag**:

- The pipeline **auto-suggests** noise for a speaker that is BOTH low-share AND
  acoustically close (by embedding) to some larger speaker — a real phantom
  (echo/cut voice) looks like this; a quiet real speaker does not.
- The operator **sees every diarized speaker** in the voice-resolution dialog,
  with the noise checkbox pre-filled from the auto-suggestion, and can toggle it.
- Speakers flagged noise are **excluded from all outputs** (raw transcript AND
  summary). Everything else survives.
- `drop_marginal_speakers` is **disabled on the live path** (never folds).

This makes the default an explicit over-split (show everyone, operator mutes
phantoms with one click) instead of a silent over-merge.

## Decisions (from brainstorming)

- **Exclusion timing:** show ALL speakers on first render; exclude noise only
  after the resolution decision (or, in auto mode, from the auto-suggestion).
- **Where excluded:** everywhere — raw transcript and summary.
- **Auto-noise criterion:** `share < min_share` AND `min(cosine to any
  larger-share speaker) <= noise_max_distance`. "Larger" = any speaker with a
  greater share, not only the single dominant one.
- **No embedding for a speaker → never auto-noise** (can't prove it's noise;
  show it to the operator).
- **Threshold:** new setting `diarization_noise_max_distance = 0.25` (separate
  from `speaker_match_max_distance_auto`; different semantics — "echo of another
  speaker here" vs "same voice in the registry").
- **Storage of the operator's decision:** `MatchDecision.is_noise` (per-task,
  per-label). The auto-value is written as the decision when the operator
  doesn't touch the checkbox — the decision row is self-contained; render reads
  only it.
- **Old tasks:** not migrated. A specific task can be re-run from the merge step
  manually.
- **Auto mode (`speaker_no_manual_stop`, no operator):** render reads noise from
  the auto-suggestion in `speaker_matches.json` (no decisions exist).
- **The dialog is available from when match_speakers first ran, for the rest of
  the task's life** (except archived/canceled) — not tied to a specific status.
  Gating is a task-DEPENDENT capability `can_resolve_speakers` (reads
  `task.steps`), NOT a pure-status predicate: the dialog needs
  `speaker_matches.json` (written by `match_speakers`), so a pure status set
  cannot express the real precondition. This lets the operator bind speakers
  while the task is still running (e.g. summarizing) without waiting for it to
  finish. See "Button availability".
- **Editing bindings/noise on a COMPLETED task is in scope** as one case of the
  above. Saving re-renders the raw transcript immediately.
- **Re-render is triggered by the resolve SAVE, not by DAG traversal.** A shared
  render function is called from the `resolve` endpoint, so it works identically
  for a paused task (continue) and a completed task (edit-in-place).
- **Summary is refreshed manually** via the existing "restart summary" button —
  saving bindings/noise does NOT auto-restart the summary (no unprompted LLM
  cost). `can_restart_summary_task` already permits `completed`, and the summary
  path already reads bindings live; it gains the noise filter.

## Architecture

The DAG is unchanged. Re-rendering is **event-driven** (triggered by the resolve
save), not a pipeline step — that is the only way it can serve BOTH a paused
task and an already-completed task with one mechanism.

```
diarize → merge_transcript → prepare_llama_model → match_speakers → summarize...
                (renders all speakers, no fold)         (pauses for dialog)

resolve save (endpoint)  ──►  rerender_transcript(task)   [always, sync]
                         ──►  re-queue pipeline           [only if continue_task]
```

Two rendering moments consume noise, via one shared source-of-truth resolver:

- **Raw transcript** — first rendered by `merge_transcript` BEFORE the dialog
  (all speakers, no fold). Then `rerender_transcript(task)` — called from the
  `resolve` endpoint on EVERY save — re-renders it, dropping noise labels and
  substituting bound names. This runs whether the task is `awaiting_input`
  (then continues down the DAG) or `completed` (edit-in-place; no re-queue).
- **Summary** — `summarization` already reads `speaker_names_for_task` live and
  gains the noise filter. It is regenerated only when the user hits "restart
  summary" (not automatically on save), so saved bindings/noise reach the
  summary on the next explicit restart.

### Re-render trigger by task status

| Task status at save | `continue_task` | rerender_transcript | re-queue DAG | summary |
|---|---|---|---|---|
| `awaiting_input` | true | yes (sync) | yes | rebuilt as DAG continues |
| `completed` | false | yes (sync) | no | stale until manual "restart summary" |

### Noise source resolver

A single repo helper decides, for a task, which labels are noise:

```
noise_labels_for_task(task_id) -> set[str]:
  decisions = MatchDecision rows for this task
  if decisions exist:            # manual mode: operator resolved
      return {label for label where is_noise}
  else:                          # auto mode: no operator
      return {label for label where speaker_matches.json[label].noise}
```

Both `rerender_transcript` and the summary path call this, so raw and
summary never disagree.

## Components

### 1. Data model / storage

- **`speaker_matches.json`** (written by `MatchSpeakersStep`): each label gains
  - `noise: bool` — the auto-suggestion
  - `share: float` — speech share by **diarization time** (0..1)
- **`MatchDecision.is_noise: bool NOT NULL DEFAULT false`** — new column
  (Alembic migration). The operator's decision; source of truth for render in
  manual mode.
- **`transcript.json` entries** — unchanged shape; `speaker` stays the technical
  `SPEAKER_NN` tag. Exclusion is a render-time choice, so it stays reversible.
- **New setting:** `diarization_noise_max_distance: float = 0.25` in `config.py`
  (+ its `services_...` structured alias, matching the existing pattern).

### 2. Auto-noise (in `MatchSpeakersStep.run`)

Embeddings and per-label diarization time are already available here.

```
share[L] = sum(seg.end-seg.start for seg in diarization if seg.speaker==L) / total_diarized_time
noise[L] = share[L] < diarization_min_speaker_share            # 0.05
           and L has an embedding
           and any B with share[B] > share[L]
               and cosine(emb[L], emb[B]) <= diarization_noise_max_distance   # 0.25
```

Verified on `9c096ee1`: SPEAKER_03 share=13.1% (>5%) → noise=false; and every
pairwise distance ≥0.54 (>0.25) → nothing folds. Bug fixed structurally.

`drop_marginal_speakers` is disabled on the live path: `MergeTranscriptStep`
passes `min_share=0.0` (with which the function is a no-op). The function and
its unit tests remain for possible reuse.

### 3. Re-render function `rerender_transcript(task, session)`

A plain function (NOT a DAG step), so it can run both mid-pipeline and on a
completed task from the same call site.

- Reads `transcript.json` entries (they already carry per-entry `speaker`),
  `speaker_names_for_task`, and `noise_labels_for_task`.
- Re-renders `transcript.json` + `transcript.txt` excluding noise-labelled
  entries and substituting registry names (via `render_cleaned_transcript` /
  `label_map`, the same renderer merge uses).
- Idempotent — safe to call on every save; the same inputs produce the same
  output. No `already_done` bookkeeping needed since it is called explicitly,
  not scheduled.
- Called from the `resolve` endpoint after decisions are committed.
- Empty-guard: if every speaker is noise, fall back to rendering all and log a
  warning rather than emit an empty transcript.
- Closes the existing gap where bound names never reached the RAW transcript tab.

### 4. API

- `SpeakerMatchOut`: add `noise: bool`, `share: float`.
- `VoiceResolution`: add `is_noise: bool = False`.
- `record_decision(...)`: add `is_noise` param → persisted on the row.
- `resolve` endpoint: after committing decisions, call `rerender_transcript`.
  Remove/relax any implicit assumption that the task is `awaiting_input` — a
  non-paused task (running/completed/failed) saving edits must succeed with
  `continue_task=false` and NOT be re-queued (status unchanged; only the raw
  transcript changes). Reject the save only when `can_resolve_speakers_task` is
  false (archived/canceled, or match_speakers not yet completed).
- `serialize_task` / `serialize_task_compact`: add
  `can_resolve_speakers: can_resolve_speakers_task(task)` to `capabilities`.
- `GET /speaker-matches` must work on any non-archived task past match_speakers
  (it already reads the static `speaker_matches.json`, so no status gate to add
  — just verify).
- No new endpoints.

### 5. Button availability — `can_resolve_speakers` capability

New task-DEPENDENT capability, computed server-side alongside
`can_restart_summary` and shipped in `capabilities`:

```
def can_resolve_speakers_task(task) -> bool:
    if task.status in {archived, canceled}:
        return False
    return _find_step_status(task, "match_speakers") == StepStatus.completed
```

- True once `match_speakers` completed, and stays true for every later status
  (running, waiting, completed, failed-after-diarization), until archived/canceled.
- The paused case (`awaiting_input`) is unaffected: there `match_speakers` is
  not yet `completed`, so the existing `needsInput && awaitingStep ===
  "match_speakers"` path drives the button. `can_resolve_speakers` covers
  everything AFTER.

### 6. Frontend (voice-resolution dialog, `app.js`)

- **Show the resolve-voices button when EITHER the paused path OR the new
  capability holds:** `needsInput(status) && awaitingStep === "match_speakers"`
  (first-time resolution at the pause) OR `capabilities.can_resolve_speakers`
  (any time after match_speakers, until archived/canceled).
- **Two buttons already exist** (`#voice-save` → `continue_task=false`,
  `#voice-save-continue` → `continue_task=true`). No relabeling, no new i18n —
  just toggle visibility of `#voice-save-continue`:
  - `awaiting_input` (paused): BOTH buttons shown. "Save" records+re-renders but
    leaves the task paused; "Save & continue" additionally re-queues the DAG.
  - any other status (running/completed/failed, via `can_resolve_speakers`):
    HIDE "Save & continue" — there is nothing to continue; only "Save"
    (records + re-renders, `continue_task=false`, pipeline untouched).
- `openVoiceDialog(taskId)` currently gets only the id; pass the task's paused
  flag (or status) so it can toggle `#voice-save-continue`. The caller (the
  resolve-button handler) already holds the task runtime.
- **Noise checkbox** per speaker row, pre-filled from `row.noise`. Checked →
  row dimmed (its turns won't reach the output); binding controls stay usable
  (a person can still be bound; unchecking restores). Included in the row's
  dirty-tracking and sent as `is_noise` in each resolution.
- **Sort speaker rows by `share` descending** (was appearance order) — biggest
  talker first.
- **Show share** per row as percent + duration (e.g. "13% · 2:29") so the
  operator sees the basis for a noise suggestion.
- **Auto hint:** when `noise` was auto-set, a small "auto: looks like
  noise/echo" note. New i18n keys in en/ru/de.
- After a save on a completed task, the summary is now stale w.r.t. the new
  bindings/noise; the existing "restart summary" button remains the way to
  regenerate it. (No new UI; just document the flow.)
- **Fetch the re-rendered transcript after save.** `submitVoiceResolutions`
  already calls `loadTasks()`, which rebuilds the task DOM and re-fetches the
  active tab — but that is an implicit dependency on `renderTasks` behaviour.
  Make it explicit: after a successful save, force-reload the transcript for
  the affected task's active tab (don't rely on the DOM rebuild). Combined with
  the server header below, the user always sees the just-rendered transcript.

### 7. Transcript cache invalidation

The raw transcript used to be immutable once written; it is now mutable (every
resolve save can change it). Two layers keep the client from showing a stale
copy:

- **Server:** the transcript endpoint (`GET /api/tasks/{id}/transcript`, via
  `_serve_text`) sends `Cache-Control: no-cache` so the browser always
  revalidates rather than serving a heuristically-cached body. (Today it sends
  only `Accept-Ranges`.) Apply the same to the summary/redacted text endpoints
  that share `_serve_text`, since they are equally mutable after a restart.
- **Client:** after a resolve save, explicitly re-fetch the active tab's content
  for that task (see the frontend note above), not merely via the incidental
  `loadTasks` rebuild.

## Error handling / edge cases

- Speaker with no embedding → never auto-noise (shown to operator).
- Auto mode with no decisions → noise from `speaker_matches.json`.
- All speakers noise (degenerate) → transcript would be empty; render must
  guard and fall back to rendering all (log a warning) rather than emit an
  empty transcript.
- Missing `speaker_matches.json` on the noise resolver → treat as no noise.
- **Concurrency (re-render vs pipeline):** `transcript.json` is written by the
  pipeline ONLY in `merge_transcript`, which runs BEFORE `match_speakers`.
  `can_resolve_speakers` is true only AFTER `match_speakers` completed, so a
  resolve-triggered re-render can never collide with the pipeline writing the
  same file — the pipeline is done with it by then. The one residual case is two
  overlapping resolve saves (double-click / two tabs). Make `rerender_transcript`
  write atomically (tmp file + `os.replace`) so a torn read is impossible;
  last-writer-wins on the decisions is acceptable (they are idempotent per
  label). Note: current `write_json` is a direct `write_text` (not atomic) — the
  re-render path must use an atomic write helper.

## Testing

Backend (pytest, real Postgres):

1. **vts-0ws regression:** 4 speakers, SPEAKER_03 diar-share 13% → survives the
   transcript; auto-noise does NOT flag it (share>5%, distance 0.555>0.25).
2. **Auto-noise unit:** close+small→noise; far+small→not; close+large→not;
   no-embedding→not.
3. **Share unit:** computed from diarization segments, not ASR-entry spans.
4. **`rerender_transcript`:** excludes noise labels, substitutes names,
   idempotent (second call same output), empty-guard fallback.
5. **Summary excludes noise:** noise-speaker entries never reach the chunks.
6. **API — resolve/paused:** `resolve` on `awaiting_input` with `is_noise=true`
   persists the decision, re-renders the raw transcript (noise dropped), and
   re-queues.
7. **API — resolve/completed:** `resolve` on a `completed` task with
   `continue_task=false` persists new decisions, re-renders the raw transcript,
   leaves status `completed`, and does NOT re-queue. `speaker-matches` returns
   `noise`+`share` for a completed task.
7b. **`can_resolve_speakers_task`:** true after match_speakers completed for
    running/waiting/completed/failed; false for archived/canceled and before
    match_speakers. `resolve` rejected when the capability is false.
7c. **Atomic re-render:** `rerender_transcript` writes via tmp+`os.replace`; a
    concurrent read never sees a partial file.

8. **Transcript endpoint sends `Cache-Control: no-cache`** (and the shared
   summary/redacted endpoints too).

Frontend (verifier-web):

9. Noise checkbox present, pre-filled from stub `noise:true`; rows sorted by
   share; share shown; toggling changes dirty state and the resolve payload.
10. Resolve-voices button visible whenever `capabilities.can_resolve_speakers`
    is set (running/completed stub) AND at the pause; hidden for
    archived/canceled and before match_speakers.
10b. Dialog buttons: on `awaiting_input` BOTH "Save" and "Save & continue" are
     visible; on a non-paused task "Save & continue" is HIDDEN and only "Save"
     shows (sends `continue_task=false`).
11. After a resolve save, the transcript tab is re-fetched (the stub serves a
    changed transcript on the second GET; the panel shows the new text without a
    page reload).

## Out of scope

- Migrating old tasks (manual merge restart if needed).
- Making noise a person-level (registry) attribute — it is per-task only.
- Version bump / deploy — handled separately after merge, as part of vts-552.
