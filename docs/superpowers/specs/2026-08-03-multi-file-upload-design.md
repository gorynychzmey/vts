# vts-vm0 — Multi-file upload processed as one recording

**Beads:** vts-vm0
**Linear:** [VOS-66](https://linear.app/vostrikov/issue/VOS-66)
**Date:** 2026-08-03
**Status:** Design approved, ready for implementation plan

## Scope

Upload several files in one go and process them as a single unit: one
transcript over the whole set, one summary over the combined transcript.

**In scope:**
- Chunked upload accepting N files under one upload session.
- Server-side ordering of the files, with no user interaction.
- Concatenation into the single audio artefact the pipeline already consumes.
- Video sets: a combined `video.mkv` so the player still works, joined by
  stream copy and refused when the parts' parameters differ.
- Rejection of mixed video+audio sets.
- File-boundary metadata (name, offset, duration) recorded on the task.
- Aggregate upload progress in the web UI.

**Out of scope, deliberately:**
- A `files` table or any change to the `Task` model (see *Data model*).
- Per-file ASR, per-file status, or per-file retry.
- Re-encoding video parts to a common resolution (see *Video sets*).
- Drag-to-reorder UI (see *Ordering*).
- Visible separators inside the transcript text (see *Boundaries*).
- Multi-file via the single-shot `POST /api/tasks/upload` path, and via MCP.

## Background — what the pipeline assumes today

The DAG is linear and single-file throughout (`vts/pipeline/types.py`):

```
download → extract_audio → trim_initial_silence → segment_audio
         → detect_language → transcribe_segments → diarize → merge_transcript → …
```

Two facts decide this design:

- `DownloadStep` no-ops for `file://` source URLs — an uploaded file is already
  in place (`vts/pipeline/steps/media.py:85`).
- `ExtractAudioStep` reads `media/audio.original.*` and writes exactly one
  `media/audio_16k.wav` (`vts/pipeline/steps/media.py:169-181`,
  `extract_audio_16k_mono` in `vts/services/media.py:60`). **Every later step
  consumes only that WAV** — segmentation, ASR, diarization
  (`ctx.transcribe_audio_path`), merge and summarization never look at the
  original upload.

## Approach: concatenate at `extract_audio`

Normalise each uploaded file to 16 kHz mono PCM, then concatenate into the one
`audio_16k.wav` the pipeline already expects. **Nothing downstream changes.**

Verified, not assumed — mp3 (44.1 kHz stereo) + wav (48 kHz mono) + ogg
(22 kHz stereo) of 2 s + 3 s + 1 s produced exactly 6.000 s after
normalise-then-concat. Heterogeneous inputs join losslessly once normalised,
and the per-file durations give exact offsets for the boundary metadata.

A rejected alternative was the issue's original sketch: a `files` table, N
independent ASR runs, and concatenated results. It costs a schema migration, a
DAG rework and per-step changes — and it is *worse* for the product: diarizing
each file separately cannot link the same speaker across files, whereas one
continuous recording links them for free.

### Failure handling

If any file fails to normalise (corrupt, unsupported), **the whole task fails**
with a message naming the file and the ffmpeg reason. Skipping a file silently
would hand back a partial transcript presented as complete, and the summary
would be built from it.

## Video sets

An upload is not necessarily audio. A single uploaded video is playable in the
web player alongside the transcript — `_find_media_file`
(`vts/api/main.py:726`) prefers `media/video.mkv` and falls back to
`media/audio.original.*`. A set of video files must therefore produce a
**combined video**, or the player would silently show one arbitrary part while
the transcript covered them all.

So `extract_audio`'s concatenation is not enough on its own: an **audio** set
needs only the combined WAV, while a **video** set additionally needs a
combined `video.mkv` for playback.

### Mixed sets are rejected

A set must be either all-video or all-audio. Mixing them has no coherent
meaning — there is no sensible combined artefact — so it is refused at `init`,
before any bytes are transferred, naming the offending files. The two groups
are already distinguishable from `_ALLOWED_UPLOAD_SUFFIXES`
(`vts/api/main.py:1543`): the first nine suffixes are video containers, the
remaining eight audio.

The extension is a cheap pre-filter, not proof: a `.mkv` may legitimately carry
no video stream. The authoritative check is the ffprobe pass that already runs
for ordering — if the probed streams disagree with the extension grouping, the
probe wins and the task fails with the reason.

### Video concat: copy when compatible, otherwise refuse

Video parts are joined with `concat -c copy` — no re-encoding — **only when
their stream parameters match**. Where they do not, the set is rejected with a
message naming the differing parameter.

This is not conservatism; stream-copy across mismatched inputs produces a
**silently corrupt file**. Measured:

| case | result of `concat -c copy` |
|---|---|
| identical params | 4.02 s for 2+2 s — correct |
| 640x480 + 1280x720 | **4.82 s**, non-monotonic DTS, output reports only 640x480 |
| 25 fps + 30 fps | **4.82 s**, non-monotonic DTS |
| audio 44.1 kHz + 48 kHz | **4.38 s**, and **ffmpeg reports no error at all** |

The last row is why the check must compare probed parameters up front rather
than trusting ffmpeg's exit code. Parameters that must match: video codec,
width, height, frame rate; audio codec, sample rate, channel count.

Re-encoding to a common resolution does work (verified: 1280x720 letterboxed,
correct 4.04 s duration) and is the obvious escape hatch — but it means CPU
`libx264` on the box that already runs diarization, with no hardware encoder in
the image. That is a heavy, open-ended step to add to the pipeline for a case
that mostly arises from deliberately mismatched sources. Deferred; if it is
wanted later, the filter-graph approach above is the known-good recipe.

The typical real case — parts of one recording from one device — has identical
parameters and takes the cheap copy path.

Note that the combined video is for **playback only**. The transcript pipeline
still consumes `audio_16k.wav`, which is built from the same sources
independently, so a video set produces both artefacts.

## Ordering

The order files are concatenated in is the order they are heard in. It is
decided **server-side at finalize**, with no user step — first-choice signal
first:

1. **`creation_time` from the container** (ffprobe). The true recording time;
   survives copying and downloading.
2. **`lastModified` from the browser**, sent per file at init. Present for
   every file.
3. **Natural sort by filename** — digit runs compared numerically.

Measured behaviour of the containers we actually receive:

| container | `creation_time` |
|---|---|
| m4a, mp3 | present |
| **ogg, opus, wav** | **absent — the container has no such tag** |

This matters: opus is what Telegram and WhatsApp voice messages use, i.e. the
most likely "several parts of one conversation" case, and for those files
`lastModified` is usually the *download* time. Step 3 is what rescues them —
verified against real patterns:

- `audio_2026-08-01_10-15-03.ogg` (Telegram) — sorts correctly
- `PTT-20260801-WA0009.opus` (WhatsApp) — sorts correctly
- `rec_9.opus` before `rec_10.opus` — which naive lexicographic sort gets wrong

`creation_time` also survives our chunked upload byte-for-byte (verified by
reassembling a split file and re-probing).

No reorder UI. Doing it properly would mean either making the user wait for the
upload before confirming an order, or confirming before any bytes move — an
extra step in the form either way. Instead the resolved order **and which rule
produced it** are shown on the task card after the fact, so a wrong order is
visible and explicable; the remedy is re-uploading.

## Data model

No schema migration. `Task` keeps one `source_url`; for a set it is
`file://<first file name>` plus a count, and the detail lives in `Task.options`:

```json
"source_files": [
  {"name": "part1.m4a", "offset_sec": 0.0,     "duration_sec": 612.4},
  {"name": "part2.m4a", "offset_sec": 612.4,   "duration_sec": 458.1}
],
"source_files_order": "creation_time"
```

`order_source` is one of `creation_time` / `last_modified` / `filename`, so the
UI can say how the order was chosen.

**`Task.options` is a JSON column and must be reassigned, never mutated in
place** — see the existing convention in the codebase.

## Boundaries

Metadata only. Nothing is injected into `transcript.txt` or `transcript.json`
text: a `--- file 2 ---` marker would reach the LLM and surface as noise in the
summary. `source_files` carries the offsets for anything that later wants to
show boundaries.

## Upload protocol

Chunked path only (`init` → `PATCH` per chunk → `finalize`). One code path
instead of two, and resumability comes for free. The single-file single-shot
path is untouched.

- `UploadInitRequest` gains `files: [{filename, total_size, last_modified}]`,
  keeping the current single-file fields for compatibility.
- `PATCH /api/uploads/{id}` gains a file index, since one session now holds N
  parts.
- **Naming collision to fix:** `_media_name()` returns a fixed
  `audio.original<suffix>` (`vts/services/upload_session.py:23`), so N files in
  one session would overwrite each other. Parts become
  `audio.original.<NN><suffix>`, `NN` being the resolved concat order.
  `ExtractAudioStep` then globs and concatenates them in name order — which is
  concat order by construction.

### Limits

- Sum of all files ≤ `max_upload_bytes` (unchanged, 2 GiB) — a set may not
  exceed what one file may.
- New `upload_max_files` (default 10), enforced at `init`.

Both rejections happen at `init`, before any bytes are transferred.

## UI

- `#file-input` gains `multiple`.
- The submit button's progress ring shows **aggregate progress across the whole
  set**, by bytes: `sent_bytes_total / sum(total_size)`.
- The task card shows the file list and the order source.

## Testing

- **Unit** — ordering resolution across the three signals, including natural
  sort (`rec_9` before `rec_10`) and the fallback chain; limit enforcement.
- **Integration** — concatenation of heterogeneous formats produces a WAV whose
  duration equals the sum, and `source_files` offsets match the real boundaries;
  a corrupt file fails the task with the file named.
- **Video** — a compatible set produces a `video.mkv` whose duration equals the
  sum and which `_find_media_file` picks up; an incompatible set is refused
  naming the differing parameter. The duration assertion matters most here:
  mismatched audio sample rates corrupt the output **without ffmpeg raising an
  error**, so a test that only checks the exit code would pass on a broken file.
- **API** — `init` rejects over-count, over-size and mixed video+audio sets
  before any chunk; `finalize` creates one task carrying `source_files`.
- **Browser (`tests/ui`)** — a multi-file selection uploads and produces one
  task card; aggregate progress advances monotonically to 100%. Each new
  scenario must be checked to fail against the pre-change code, per the
  existing convention in this repo.

## Risks

- **Ordering is unfixable without re-upload.** Accepted: the order and its
  source are shown, so a wrong result is at least explicable.
- **Peak disk doubles** during extract (originals + combined WAV), and for a
  video set roughly triples (originals + `video.mkv` + WAV). Bounded by the
  2 GiB set limit.
- **Video sets from mixed sources are refused**, not repaired. Parts recorded on
  one device match and pass; a set assembled from different devices or
  re-encoded by a messenger may not, and the user gets a refusal rather than a
  result. The message names the differing parameter so it is actionable, and
  re-encoding remains available as a later addition.
- **Long recordings.** A 2 GiB set is a long meeting; diarization is the slow
  step and now runs on a longer input. Bounded by the same limit, and helped by
  the 3.62x diarization speedup already shipped (vts-zrz).
