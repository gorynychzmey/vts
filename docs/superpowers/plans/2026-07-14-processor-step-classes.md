# Processor Step-Classes Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `vts/pipeline/processor.py` (~2494 lines) into a thin `TaskProcessor` orchestrator plus 12 `Step` classes under `vts/pipeline/steps/`, with shared infra behind an explicit `PipelineContext` — with ZERO behavior change.

**Architecture:** A `PipelineContext` holds services (llm, whisper, bus, lanes, settings, session_factory, metrics) and cross-domain infra helpers. Each pipeline step becomes a `Step` subclass with `run(ctx, state)` and `already_done(ctx, state)`; a registry + `resolve_step(name)` replaces the `getattr(self, f"step_{name}")` dispatch. Domain-pure helpers become module functions beside their steps; finalize's system/user difference lives in a `PromptSource` strategy, not a class hierarchy.

**Tech Stack:** Python 3.12 asyncio, SQLAlchemy async, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-14-processor-step-classes-design.md`

## Global Constraints

- **Behavior-preserving refactor.** No change to step behavior, DAG order, task/step statuses, SSE events, resume semantics, night mode, or lane priorities. The verification for EVERY task is: `pytest -q` stays fully green AND the diff only moves/renames code — it never alters control flow or values.
- **Public surface unchanged:** `from vts.pipeline.processor import TaskProcessor` works; `TaskProcessor.__init__(*, session_factory, redis, settings, lanes=None)` and `process_task(task_id)` signatures unchanged; `TaskPaused` stays importable from `vts.pipeline.processor`.
- **Test suite is the safety net.** Run the FULL suite after every task: `/home/victor/dev/vts/.venv/bin/python -m pytest -q` (python is NOT on PATH; always use the venv path). Baseline before starting: **628 passed** (Task 1 adds 3, so after Task 1 expect 631; count only rises as white-box tests split — never drops).
- **The 6 white-box tests** call `TaskProcessor.step_*`/`_run_step`/`_get_n_ctx` directly (via `TaskProcessor.__new__`) and stub `processor._llm/.lanes/.bus/._effective_language/._task_n_ctx` etc. Each is rewritten onto the new Step/ctx surface in the SAME task that moves the method it exercises — asserting the same behavior, not weakened:
  - `test_processor_lanes`, `test_task_title_preservation`, `test_pipeline_resume` (extract_audio dry-run part) → Task 3 (media)
  - `test_segmentation_mode` (transcription-touching stubs) → Task 4; (summary stubs) → Task 5
  - `test_finalize_loop`, `test_pipeline_resume` (summarize_windows parts) → Task 5 (summarization)
  - **`test_llm_backends`** calls `proc._get_n_ctx(...)` and monkeypatches `vts.pipeline.processor.discover_n_ctx`; since `_get_n_ctx` moves to `PipelineContext.get_n_ctx` (Task 1), rewrite it in **Task 1** onto a `PipelineContext` built via `__new__`/stub, monkeypatching `vts.pipeline.context.discover_n_ctx`, asserting the same `n_ctx==114688` and cache behavior.
- **No `dry_run` bool param** in the new surface — replaced by `Step.already_done()`. **No `lane_for_step()`** — replaced by `Step.lane` attribute; delete `lane_for_step`/`STEP_LANES` from `vts/pipeline/types.py` (keep `build_dag_steps`/`DAG_HEAD`/`finalize_step_name`).
- Version bump in `vts/__init__.py` happens ONCE, in the final task. Docs/spec commits never bump.
- Commit after every task.

## File Structure (target)

```
vts/pipeline/
  processor.py          # thin: TaskProcessor (process_task, _run_step dispatch, donor-clone, PipelineContext construction)
  context.py            # PipelineContext (services + infra helpers)
  types.py              # build_dag_steps/DAG_HEAD/finalize_step_name (lane_for_step/STEP_LANES removed)
  steps/
    __init__.py
    base.py             # Step ABC, StepState
    registry.py         # STEP_REGISTRY, resolve_step()
    media.py            # DownloadStep, ExtractAudioStep, TrimInitialSilenceStep, SegmentAudioStep
    transcription.py    # DetectLanguageStep, TranscribeSegmentsStep, MergeTranscriptStep + ASR domain helpers
    summarization.py    # PrepareLlamaModelStep, PrepareSummaryChunksStep, SummarizeWindowsStep, PackWindowNotesStep, FinalizePromptStep, PromptSource
```

---

### Task 1: PipelineContext + Step ABC + StepState + empty registry (scaffold)

**Model:** Opus 4.8 — defines the interface every later task consumes; getting the ctx boundary right is the crux.

**Files:**
- Create: `vts/pipeline/context.py`, `vts/pipeline/steps/__init__.py`, `vts/pipeline/steps/base.py`, `vts/pipeline/steps/registry.py`
- Modify: `vts/pipeline/processor.py` (construct a `PipelineContext` in `__init__`; keep all existing methods intact for now)
- Test: `tests/test_pipeline_context.py` (new)

**Interfaces:**
- Produces — `vts/pipeline/steps/base.py`:
  ```python
  @dataclass
  class StepState:
      task_id: uuid.UUID
      user_id: str
      dirs: dict[str, Path]
      logger: logging.Logger
      task_options: dict[str, Any]

  class Step(ABC):
      name: str
      lane: str | None = None
      async def run(self, ctx: "PipelineContext", st: StepState) -> None: ...      # abstractmethod
      async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool: return False
  ```
- Produces — `vts/pipeline/context.py` `PipelineContext`. It is constructed from a `TaskProcessor` and exposes these services as attributes: `llm`, `whisper`, `bus`, `lanes`, `settings`, `session_factory`. Plus these async/sync infra methods (moved verbatim in logic from the identically-named `TaskProcessor._*` methods — SAME signatures minus `self`):
  - `gpu_slot(task_id, user_id, cls)` → async CM
  - `refresh_task(session, task)` (raises `_TaskGone` — keep `_TaskGone` in processor.py and import it, OR move `_TaskGone`+`TaskPaused` into context.py; DECISION: keep both exception classes in `processor.py` and import them into context.py, since `TaskPaused` is part of the public surface)
  - `mark_waiting(task_id, user_id, queue)`, `mark_running(task_id, user_id)`
  - `check_paused(task_id)`
  - `get_emitter(task_id) -> MetricsEmitter | None`
  - `get_n_ctx(task_id, logger) -> int`
  - `persist_summary_progress(task_id, current, total)`
  - `persist_detected_language(task_id, language, confidence)`
  - `save_task_source_title(task_id, title)`
  - `get_user_preferred_ytdlp_client(user_id)`, `set_user_preferred_ytdlp_client(user_id, player_client)`
  - `task_url(task_id) -> str`
  - `send_push_safe(session, user_id, payload)`
- Produces — `vts/pipeline/steps/registry.py`: `STEP_REGISTRY: dict[str, Step]` (empty for now) and `def resolve_step(step_name: str) -> Step` raising `KeyError` on unknown names for now (real logic lands as steps register).

- [ ] **Step 1: Write failing test** — `tests/test_pipeline_context.py`:
```python
import uuid
from vts.pipeline.steps.base import Step, StepState

def test_step_state_holds_fields():
    st = StepState(task_id=uuid.uuid4(), user_id="u", dirs={}, logger=None, task_options={})
    assert st.user_id == "u"

def test_step_defaults():
    class _S(Step):
        name = "x"
        async def run(self, ctx, st): return None
    assert _S().lane is None

import asyncio
def test_already_done_defaults_false():
    class _S(Step):
        name = "x"
        async def run(self, ctx, st): return None
    assert asyncio.get_event_loop().run_until_complete(_S().already_done(None, None)) is False
```
- [ ] **Step 2: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_pipeline_context.py -q` — expect FAIL (module missing).
- [ ] **Step 3: Implement** `base.py` (exact classes from Interfaces). Implement `context.py`: a `PipelineContext` whose `__init__(self, proc)` copies `proc.session_factory/redis/settings/bus/lanes/whisper/_llm/_task_metrics/_task_n_ctx` into attributes (`llm = proc._llm`, `whisper = proc.whisper`, …) and whose infra methods are the bodies of the corresponding `TaskProcessor._*` methods with `self.` rewritten to the ctx attributes. Copy the method BODIES verbatim (read them at their line numbers), changing only attribute references (`self._llm` → `self.llm`, `self.session_factory` → `self.session_factory`, `self._get_emitter` → `self.get_emitter`, etc.). Do NOT yet delete the originals from `TaskProcessor` — Task 6 does that. In `TaskProcessor.__init__`, after building services, add `self._ctx = PipelineContext(self)`. Implement `registry.py` stub.
- [ ] **Step 4: Rewrite `test_llm_backends`'s n_ctx test.** It calls `proc._get_n_ctx(...)` via `TaskProcessor.__new__` and monkeypatches `vts.pipeline.processor.discover_n_ctx`. Since `get_n_ctx` now lives on `PipelineContext` (and imports `discover_n_ctx` from `vts.pipeline.context`), rewrite the test to build a `PipelineContext` via `PipelineContext.__new__(PipelineContext)` with `settings`/`_task_n_ctx` (or `n_ctx` cache dict) stubbed, monkeypatch `vts.pipeline.context.discover_n_ctx`, and assert `await ctx.get_n_ctx(task_id, logger) == 114688` and the cache is populated. Keep `discover_n_ctx` importable from BOTH modules during migration if the original processor method is not yet deleted (Task 6 deletes it) — so leave `processor.discover_n_ctx` import intact until Task 6.
- [ ] **Step 5: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_pipeline_context.py tests/test_llm_backends.py -q && /home/victor/dev/vts/.venv/bin/python -m pytest -q` — all PASS.
- [ ] **Step 6: Commit** `feat(pipeline): PipelineContext + Step ABC scaffold (vts-08d)`

---

### Task 2: Dispatch via registry (behind a feature-equivalent path)

**Model:** Opus 4.8 — rewires the hot dispatch path; must preserve skip/lane/status/event ordering exactly.

**Files:**
- Modify: `vts/pipeline/processor.py` (`_run_step` ~431-533), `vts/pipeline/steps/registry.py`
- Test: existing suite (no new test; `test_processor_lanes` covers `_run_step` and is rewritten in Task 3 when media steps move)

**Interfaces:**
- Consumes: `Step`, `StepState`, `resolve_step` (Task 1).
- Produces: `resolve_step(step_name)` final logic:
  ```python
  def resolve_step(step_name: str) -> Step:
      if step_name == "summarize_final":
          return FinalizePromptStep(source="system", id="summary")
      if step_name.startswith("finalize:"):
          src, rid = parse_ref(step_name.split(":", 1)[1])
          return FinalizePromptStep(source=src, id=rid)
      return STEP_REGISTRY[step_name]
  ```
  (FinalizePromptStep import is added in Task 5; until then keep the finalize branches dispatching to the OLD `self.step_finalize_prompt` — see Step 3.)

- [ ] **Step 1: Transitional dispatch.** Because steps move incrementally (Tasks 3-5), `_run_step` must dispatch registered steps via the registry AND fall back to the old `getattr` for not-yet-moved steps. Rewrite `_run_step`'s method-resolution + call section so it does:
```python
        st = StepState(task_id=task_id, user_id=user_id, dirs=dirs, logger=logger, task_options=task_options)
        step_obj = None
        try:
            step_obj = resolve_step(step_name)   # registry; KeyError if not yet migrated
        except KeyError:
            step_obj = None

        if step_obj is not None:
            if step.status == StepStatus.completed and await step_obj.already_done(self._ctx, st):
                return
            lane = step_obj.lane
        else:
            # legacy path for not-yet-migrated steps (removed in Task 6)
            if step_name == "summarize_final":
                method = functools.partial(self.step_finalize_prompt, source="system", id="summary")
            elif step_name.startswith("finalize:"):
                f_source, f_id = parse_ref(step_name.split(":", 1)[1])
                method = functools.partial(self.step_finalize_prompt, source=f_source, id=f_id)
            else:
                method = getattr(self, f"step_{step_name}")
            if step.status == StepStatus.completed and await method(task_id, user_id, dirs, logger, task_options, dry_run=True):
                return
            lane = lane_for_step(step_name)
```
Then the lane-acquire block stays, and the run call becomes:
```python
        async with lane_cm:
            await repo.set_step_status(step, StepStatus.running)
            await session.commit()
            await self.bus.publish_event(..., data={"name": step_name, "status": StepStatus.running.value})
            _step_t0 = time.monotonic()
            try:
                if step_obj is not None:
                    await step_obj.run(self._ctx, st)
                else:
                    await method(task_id, user_id, dirs, logger, task_options, dry_run=False)
                ...unchanged completed/failed handling...
```
Keep `resolve_step` returning `FinalizePromptStep` ONLY after Task 5; until then `finalize`/`summarize_final` fall through `KeyError` to the legacy branch (so `resolve_step` must raise KeyError for finalize names until Task 5 — implement it to `raise KeyError` for finalize until FinalizePromptStep exists, then swap in Task 5).
- [ ] **Step 2: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS (registry empty, everything still uses legacy path; behavior identical).
- [ ] **Step 3: Commit** `refactor(pipeline): registry-aware _run_step dispatch with legacy fallback (vts-08d)`

---

### Task 3: Media steps → steps/media.py + rewrite lane/title tests

**Model:** Opus 4.8 — first real step migration; sets the pattern for 4-5.

**Files:**
- Create: `vts/pipeline/steps/media.py`
- Modify: `vts/pipeline/processor.py` (remove the 4 media `step_*` methods once moved), `vts/pipeline/steps/registry.py` (register 4 steps)
- Rewrite: `tests/test_processor_lanes.py`, `tests/test_task_title_preservation.py`, and the `step_extract_audio` dry-run test in `tests/test_pipeline_resume.py`

**Interfaces:**
- Consumes: `Step`, `StepState`, `PipelineContext`.
- Produces: `DownloadStep` (`name="download"`, `lane="network"`), `ExtractAudioStep` (`name="extract_audio"`, `lane="ffmpeg"`), `TrimInitialSilenceStep` (`name="trim_initial_silence"`, `lane="ffmpeg"`), `SegmentAudioStep` (`name="segment_audio"`, `lane="ffmpeg"`). Each registered in `STEP_REGISTRY`.

- [ ] **Step 1: Move the 4 media step methods** (`step_download` 534-625, `step_extract_audio` 626-664, `step_trim_initial_silence` 665-713, `step_segment_audio` 714-801) into `media.py` as `Step` subclasses. Mechanical translation rule: the method body becomes `run(self, ctx, st)`; the `dry_run=True` early-return branch becomes `already_done(self, ctx, st)`; rename locals `task_id→st.task_id`, `user_id→st.user_id`, `dirs→st.dirs`, `logger→st.logger`, `task_options→st.task_options`; rewrite `self._effective_language(...)` etc. — for infra helpers use `ctx.<name>(...)`; for domain-pure helpers used by media (none major) inline or import. `self._task_flag`/`self._task_options` are pipeline-wide pure helpers — move `_task_flag` to `context.py` as `ctx.task_flag(...)` (or a module function in `base.py`); pick one and use consistently. Set `lane` per the table. Register all four in `STEP_REGISTRY`.
- [ ] **Step 2: Delete** the 4 moved `step_*` methods from `processor.py`.
- [ ] **Step 3: Rewrite `tests/test_processor_lanes.py`.** It currently sets `proc.step_download = _step` and calls `proc._run_step(...)`. Replace with: register a fake `DownloadStep` whose `run` records enter/exit (monkeypatch `STEP_REGISTRY["download"]` or the module's `DownloadStep`), keep driving through `proc._run_step` (dispatch is what's under test). Assert the SAME two properties: the second concurrent download emits a `waiting` event with `queue=="network"`, and the two never overlap. Keep the `_gpu_slot` waiting/running test as-is if it still uses `_run_step`; if it referenced a removed method, point it at the registry step.
- [ ] **Step 4: Rewrite `tests/test_task_title_preservation.py`** (drives `step_download` for the yt-dlp title save) onto `DownloadStep().run(ctx, st)` with a stubbed ctx (ctx.save_task_source_title captured), asserting the title is saved exactly as before.
- [ ] **Step 5: Rewrite the `step_extract_audio` dry-run test** in `test_pipeline_resume.py` onto `ExtractAudioStep().already_done(ctx, st)`.
- [ ] **Step 6: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS.
- [ ] **Step 7: Commit** `refactor(pipeline): media steps to steps/media.py (vts-08d)`

---

### Task 4: Transcription steps + ASR domain helpers → steps/transcription.py

**Model:** Opus 4.8 — carries the ASR helper cluster; the `transcribe_segments` step is large (~180 lines) with in-body gpu-slot acquisition.

**Files:**
- Create: `vts/pipeline/steps/transcription.py`
- Modify: `vts/pipeline/processor.py` (remove 3 step methods + ASR helpers once moved), `vts/pipeline/steps/registry.py`
- Rewrite: `tests/test_segmentation_mode.py` parts that touch transcription (if any); otherwise none

**Interfaces:**
- Produces: `DetectLanguageStep` (`name="detect_language"`, `lane=None` — gpu slot acquired in-body per call), `TranscribeSegmentsStep` (`name="transcribe_segments"`, `lane=None`), `MergeTranscriptStep` (`name="merge_transcript"`, `lane=None`). Registered.
- ASR domain helpers become MODULE FUNCTIONS in `transcription.py` (pure): `is_probable_asr_hallucination(text)`, `transcript_quality_score(text)`, `trim_repetitive_edges(text)`, `normalize_token(value)`, `tail_prompt(text, max_chars=800)`, `transcribe_audio_path(dirs)`, `effective_language(task_options, dirs)`, `normalize_language(value)`. `effective_language`/`normalize_language` live here and are imported by summarization.py (per spec's shared-helper note).

- [ ] **Step 1: Move ASR helpers** (`_is_probable_asr_hallucination` 2248, `_transcript_quality_score` 2290, `_trim_repetitive_edges` 2304, `_normalize_token` 2301, `_tail_prompt` 2465, `_transcribe_audio_path` 2203, `_effective_language` 2215, `_normalize_language` 2209) into `transcription.py` as module functions (drop `self`, keep bodies verbatim; `_normalize_token` is called by `_trim_repetitive_edges`/`_is_probable...` — keep the call as a module-local function call).
- [ ] **Step 2: Move the 3 transcription step methods** (`step_detect_language` 802-895, `step_transcribe_segments` 896-1078, `step_merge_transcript` 1079-1139) into `Step` subclasses using the same translation rule as Task 3. In-body `async with self._gpu_slot(task_id, user_id, "asr")` → `async with ctx.gpu_slot(st.task_id, st.user_id, "asr")`. `self._effective_language(...)` → `effective_language(...)` (module fn). `self._persist_detected_language(...)` → `ctx.persist_detected_language(...)`. Register the 3 steps.
- [ ] **Step 3: Delete** the moved methods/helpers from `processor.py`.
- [ ] **Step 4: Fix `test_segmentation_mode.py`** stubs that referenced `processor._effective_language`/`_render_prompt_with_language` — those become module functions; monkeypatch them at `vts.pipeline.steps.transcription.effective_language` / `vts.pipeline.steps.summarization.render_prompt_with_language` (summary one moves in Task 5, so if this test needs it, note the cross-task dependency and land the monkeypatch-path update in Task 5). If the test only drives summary steps, no transcription change needed here.
- [ ] **Step 5: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS.
- [ ] **Step 6: Commit** `refactor(pipeline): transcription steps + ASR helpers to steps/transcription.py (vts-08d)`

---

### Task 5: Summarization steps + PromptSource + summary helpers → steps/summarization.py

**Model:** Opus 4.8 — largest cluster (5 steps incl. ~300-line summarize_windows and ~230-line finalize), plus the PromptSource strategy.

**Files:**
- Create: `vts/pipeline/steps/summarization.py`
- Modify: `vts/pipeline/processor.py` (remove 5 step methods + summary helpers), `vts/pipeline/steps/registry.py`, `vts/pipeline/steps/registry.py` finalize branches (now import `FinalizePromptStep`)
- Rewrite: `tests/test_finalize_loop.py`, `tests/test_segmentation_mode.py`, summarize-windows tests in `tests/test_pipeline_resume.py`

**Interfaces:**
- Produces steps: `PrepareLlamaModelStep` (`name="prepare_llama_model"`, `lane=None`), `PrepareSummaryChunksStep` (`name="prepare_summary_chunks"`, `lane=None`), `SummarizeWindowsStep` (`name="summarize_windows"`, `lane=None`), `PackWindowNotesStep` (`name="pack_window_notes"`, `lane=None`), `FinalizePromptStep(source: str, id: str)` (`name="finalize"` conceptual; NOT in STEP_REGISTRY — created by `resolve_step`). Register the first four.
- Produces `PromptSource`:
  ```python
  class PromptSource(Protocol):
      async def load_text(self, ctx, id, output_language, user_id) -> str: ...
      async def display_name(self, ctx, id, user_id) -> str: ...
  # SystemPromptSource, UserPromptSource (UUID-validates id); prompt_source_for(source) -> PromptSource
  ```
- Summary domain helpers become module functions/step-methods: `token_budget_config(settings, n_ctx)`, `render_prompt_budget_vars(...)`, `render_prompt_with_language(prompt, language)`, `language_display_name(language)`, `extract_window_text(window)`, `log_metrics(logger, metrics)`. `persist_prompt_result` stays behind `ctx` (touches DB) → `ctx.persist_prompt_result(...)` (move it into context.py in Task 1's set OR add here; DECISION: add `persist_prompt_result` to `PipelineContext` in THIS task since only finalize uses it). `_token_budget_config`/`_render_prompt_budget_vars` reference `self.settings`/`self._tokenizer_path` → take `settings` param or `ctx`.

- [ ] **Step 1: Move summary helpers** (`_token_budget_config` 198, `_render_prompt_budget_vars` 239, `_render_prompt_with_language` 2233, `_language_display_name` 2237, `_extract_window_text` 2107, `_log_metrics` 228, `_tokenizer_path` 224) into `summarization.py` as module functions (thread `settings` where they used `self.settings`). Move `resolve_prompt_text` (1818) and `_prompt_display_name` (1847) logic INTO `SystemPromptSource`/`UserPromptSource.load_text`/`display_name` (the `if source=="system"` branch → System impl, else → User impl; the user-id UUID validation from `step_finalize_prompt` lines ~1889-1895 → `UserPromptSource`). Add `persist_prompt_result` to `PipelineContext`.
- [ ] **Step 2: Move the 5 summarization step methods** (`step_prepare_llama_model` 1140, `step_prepare_summary_chunks` 1212, `step_summarize_windows` 1301, `step_pack_window_notes` 1607, `step_finalize_prompt` 1876) into `Step` subclasses. `FinalizePromptStep.__init__(self, source, id)` stores them; `run` calls `prompt_source_for(self.source).load_text(...)`/`.display_name(...)` instead of the inline `if source==...`. In-body `async with self._gpu_slot(task_id, user_id, "llm")` → `ctx.gpu_slot(...)`. `self._get_n_ctx` → `ctx.get_n_ctx`, `self._persist_summary_progress` → `ctx.persist_summary_progress`, `self._get_emitter` → `ctx.get_emitter`, `self._log_payload` → keep as module fn or ctx (used by many; put `log_payload` in `base.py` as a module function). Register the 4 non-finalize steps.
- [ ] **Step 3: Wire finalize into the registry.** In `registry.py`, import `FinalizePromptStep` and make `resolve_step` return it for `summarize_final`/`finalize:*` (replacing the Task-2 KeyError-for-finalize behavior).
- [ ] **Step 4: Delete** the moved methods/helpers from `processor.py`.
- [ ] **Step 5: Rewrite the summarization white-box tests.** `test_pipeline_resume.py` (`step_summarize_windows` resume + dry-run) → `SummarizeWindowsStep().run/already_done(ctx, st)` with stubbed `ctx.llm`, `ctx.get_n_ctx`, `ctx.persist_summary_progress`, `ctx.get_emitter`; assert the same event counts / resume behavior. `test_finalize_loop.py` → `FinalizePromptStep(source, id).run(ctx, st)`; the test's `_bind` helper that mirrored `_run_step` finalize dispatch is replaced by asserting `resolve_step("summarize_final")` and `resolve_step("finalize:user:<uuid>")` produce a `FinalizePromptStep` with the right `source`/`id`. `test_segmentation_mode.py` → `PrepareSummaryChunksStep`/`SummarizeWindowsStep` via ctx; update monkeypatch paths to `vts.pipeline.steps.summarization.*` and `vts.pipeline.steps.transcription.effective_language`.
- [ ] **Step 6: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS.
- [ ] **Step 7: Commit** `refactor(pipeline): summarization steps + PromptSource to steps/summarization.py (vts-08d)`

---

### Task 6: Remove legacy dispatch, dead helpers, lane_for_step; thin processor

**Model:** Opus 4.8 — final consolidation; must confirm nothing dangles.

**Files:**
- Modify: `vts/pipeline/processor.py` (remove legacy branch in `_run_step`, remove infra `_*` methods now living in `PipelineContext`, remove `_get_emitter`/`_get_n_ctx`/`_persist_*`/`_mark_*`/`_gpu_slot`/`_check_paused`/`_refresh_task`/`_send_push_safe`/`_task_url`/`_get/set_user_preferred_*` IF `process_task` no longer calls them directly — see Step 1), `vts/pipeline/types.py` (delete `lane_for_step`, `STEP_LANES`)

**Interfaces:**
- Consumes: everything from Tasks 1-5.

- [ ] **Step 1: Decide processor vs ctx ownership for `process_task`'s own calls.** `process_task` itself calls `self._check_paused`, `self._refresh_task`, `self._cleanup_media`, `self._send_push_safe`, `self._task_metrics`/`_task_n_ctx`, `self._task_logger`, `self._is_step_enabled`, `self._task_options`, `self._clone_from_donor`. These stay on `TaskProcessor` (orchestration). For the ones ALSO needed as ctx infra (`check_paused`, `refresh_task`, `send_push_safe`, `get_emitter`, `get_n_ctx`, `mark_*`, `gpu_slot`, `persist_*`, `task_url`, `get/set_user_preferred_*`): keep the canonical impl in `PipelineContext`, and where `process_task` needs them, call `self._ctx.<name>(...)`. Remove the now-duplicate `TaskProcessor._*` copies created as "don't delete yet" in Task 1. Verify by grep that no `self._<moved>` reference remains in `processor.py`.
- [ ] **Step 2: Remove the legacy `getattr`/`functools.partial` branch** from `_run_step` (every step now resolves via the registry; `resolve_step` raising `KeyError` now signals a real bug, so let it propagate). Remove `import functools` if unused; remove `lane_for_step` import.
- [ ] **Step 3: Delete `lane_for_step` and `STEP_LANES`** from `vts/pipeline/types.py`. Grep the repo: `grep -rn "lane_for_step\|STEP_LANES" vts/ tests/` must show zero hits after (the Step.lane attribute replaced them).
- [ ] **Step 4: Confirm thinness.** `wc -l vts/pipeline/processor.py` should be ~400-600 (down from 2494). Grep `def step_` in processor.py → zero.
- [ ] **Step 5: Run** `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — PASS (full suite green).
- [ ] **Step 6: Commit** `refactor(pipeline): remove legacy dispatch + lane_for_step; thin TaskProcessor (vts-08d)`

---

### Task 7: Final gates + version bump

**Model:** Sonnet 5 — checklist execution.

- [ ] **Step 1:** Bump `vts/__init__.py` (minor: internal refactor, no behavior change — 1.2.1 → 1.3.0). (Refactor with no user-facing change; a minor bump signals the internal restructuring; confirm with the version currently in the file at execution time and increment the minor.)
- [ ] **Step 2:** Full `/home/victor/dev/vts/.venv/bin/python -m pytest -q` — all green; capture the pass count and confirm it equals baseline + net new tests (no dropped coverage).
- [ ] **Step 3:** Behavior-equivalence check: if an LLM/whisper environment is reachable, run one short video end-to-end via the `verify` skill and confirm transcript+summary produced; if not reachable, state that the guarantee rests on the full suite and the mechanical-move invariant, and note it in the commit body.
- [ ] **Step 4:** Grep guards: `grep -rn "def step_" vts/pipeline/processor.py` → empty; `grep -rn "lane_for_step\|STEP_LANES" vts/ tests/` → empty; `grep -rn "dry_run" vts/pipeline/` → only inside step `already_done` history if any (should be none).
- [ ] **Step 5: Commit** `refactor(pipeline): Step classes + PipelineContext (vts-08d)` + version bump; `git pull --rebase && bd dolt pull && bd dolt push && git push`.
- [ ] **Step 6:** bd: close vts-08d after Victor confirms; mirror the refactor's key facts to Cognee `development_knowledge` (per Knowledge Capture rule). No build tag unless Victor asks.
