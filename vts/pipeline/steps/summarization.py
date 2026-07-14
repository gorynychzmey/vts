from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from vts.core.config import Settings
from vts.db.repo import Repo
from vts.pipeline.steps.base import Step, StepState, log_payload
from vts.pipeline.steps.transcription import effective_language
from vts.pipeline.token_budget import (
    TokenBudgetConfig,
    SummarizationMetrics,
    compute_final_budget,
    compute_pack_budget,
    compute_segment_budget,
    derive_window_tokens,
    fits_in_context,
    fits_whole_transcript,
    is_context_overflow_error,
    uncap_segment_for_input,
    whole_transcript_possible,
)
from vts.services.prompt_registry import list_system_prompts
from vts.services.summarizer import (
    inject_budget_vars,
    load_prompt,
    parse_json_response,
)
from vts.services.storage import write_json
from vts.metrics import QualityAnalyzer

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class _WholeTranscriptOverflow(Exception):
    """Whole-transcript rewrite hit a context overflow; carries the cause."""


_SEGMENT_PROMPT_FALLBACK = (
    "Rewrite the transcript segment as clean fluent text: remove fillers,"
    " interjections, false starts and repetitions, but keep all content,"
    " wording and order. Do not summarize."
)


# --- summary domain helpers (pure module functions) -------------------------


def language_display_name(language: str | None) -> str:
    lang = (language or "en").strip().lower()
    mapping = {
        "en": "English",
        "ru": "Russian",
        "de": "German",
        "fr": "French",
        "es": "Spanish",
    }
    return mapping.get(lang, lang)


def render_prompt_with_language(prompt: str, language: str | None) -> str:
    value = language_display_name(language)
    return prompt.replace("${LANG}", value)


def tokenizer_path(settings: Settings) -> str | None:
    p = settings.llm_tokenizer_path
    return str(p) if p is not None else None


def token_budget_config(settings: Settings, n_ctx: int) -> TokenBudgetConfig:
    _defaults = TokenBudgetConfig()
    s = settings

    def _get(name: str, default: object) -> object:
        return getattr(s, f"summary_{name}", default)

    return TokenBudgetConfig(
        n_ctx=n_ctx,
        safety_margin=int(_get("safety_margin", _defaults.safety_margin)),
        segment_ratio=float(_get("segment_ratio", _defaults.segment_ratio)),
        segment_min_ratio=float(_get("segment_min_ratio", _defaults.segment_min_ratio)),
        segment_max_ratio=float(_get("segment_max_ratio", _defaults.segment_max_ratio)),
        segment_min_floor=int(_get("segment_min_floor", _defaults.segment_min_floor)),
        segment_max_cap=int(_get("segment_max_cap", _defaults.segment_max_cap)),
        pack_ratio=float(_get("pack_ratio", _defaults.pack_ratio)),
        pack_min_ratio=float(_get("pack_min_ratio", _defaults.pack_min_ratio)),
        pack_max_ratio=float(_get("pack_max_ratio", _defaults.pack_max_ratio)),
        pack_min_floor=int(_get("pack_min_floor", _defaults.pack_min_floor)),
        pack_batch_max_input_tokens=int(_get("pack_batch_max_input_tokens", _defaults.pack_batch_max_input_tokens)),
        final_ratio=float(_get("final_ratio", _defaults.final_ratio)),
        final_min_ratio=float(_get("final_min_ratio", _defaults.final_min_ratio)),
        final_max_ratio=float(_get("final_max_ratio", _defaults.final_max_ratio)),
    )


def log_metrics(logger: logging.Logger, metrics: SummarizationMetrics) -> None:
    logger.info(
        "token_budget stage=%s input=%d target=%d actual=%d packing=%s pass_count=%d",
        metrics.stage_name,
        metrics.input_tokens,
        metrics.target_tokens,
        metrics.actual_output_tokens,
        metrics.packing_triggered,
        metrics.packing_pass_count,
    )


def render_prompt_budget_vars(
    prompt: str,
    *,
    language: str | None = None,
    input_tokens: int | None = None,
    target_tokens: int | None = None,
    target_ratio: float | None = None,
) -> str:
    if language is not None:
        prompt = render_prompt_with_language(prompt, language)
    prompt = inject_budget_vars(
        prompt,
        input_tokens=input_tokens,
        target_tokens=target_tokens,
        target_ratio=target_ratio,
    )
    return prompt


def extract_window_text(window: dict[str, Any]) -> str:
    summary = window.get("summary", {})
    if isinstance(summary, str):
        return summary.strip()
    if not isinstance(summary, dict):
        return str(summary).strip()
    # Legacy JSON dict summaries — check for raw/summary keys first
    for key in ("summary", "raw"):
        val = summary.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Legacy structured JSON summary — render as readable text
    parts: list[str] = []
    for key, val in summary.items():
        if key == "raw":
            continue
        if isinstance(val, list):
            parts.append(f"{key}: " + "; ".join(str(i) for i in val))
        elif isinstance(val, str) and val.strip():
            parts.append(f"{key}: {val.strip()}")
    return "\n".join(parts)


# --- PromptSource strategy ---------------------------------------------------


class PromptSource(Protocol):
    async def load_text(self, ctx, id, output_language, user_id) -> str: ...
    async def display_name(self, ctx, id, user_id) -> str: ...


class SystemPromptSource:
    """Registered system prompts (e.g. ``system/summary``)."""

    async def load_text(self, ctx, id, output_language, user_id) -> str:
        sysdef = next((p for p in list_system_prompts() if p.key == id), None)
        if sysdef is None:
            raise RuntimeError(f"unknown system prompt: {id}")
        return render_prompt_with_language(
            load_prompt(
                ctx.settings.prompts_dir,
                sysdef.file,
                "Produce a structured knowledge document from the notes.\n\nOutput language: ${LANG}.",
            ),
            output_language,
        )

    async def display_name(self, ctx, id, user_id) -> str:
        sysdef = next((p for p in list_system_prompts() if p.key == id), None)
        return sysdef.i18n_name_key if sysdef else id


class UserPromptSource:
    """User-authored prompts stored in the DB (id is a UUID)."""

    async def load_text(self, ctx, id, output_language, user_id) -> str:
        # Defense-in-depth: validate the id BEFORE it is used to build any result
        # path. A user-source id must be a UUID; this rejects path-traversal ids
        # (e.g. "../../etc/passwd") regardless of downstream call ordering.
        try:
            uuid.UUID(id)
        except (ValueError, TypeError):
            raise RuntimeError(f"invalid user prompt id: {id!r}")
        async with ctx.session_factory() as session:
            repo = Repo(session)
            row = await repo.get_prompt(uuid.UUID(user_id), uuid.UUID(id))
        if row is None:
            raise RuntimeError(f"user prompt not found: {id}")
        return render_prompt_with_language(row.system_prompt, output_language)

    async def display_name(self, ctx, id, user_id) -> str:
        async with ctx.session_factory() as session:
            repo = Repo(session)
            row = await repo.get_prompt(uuid.UUID(user_id), uuid.UUID(id))
        return row.name if row is not None else id


def prompt_source_for(source: str) -> PromptSource:
    if source == "system":
        return SystemPromptSource()
    return UserPromptSource()


# --- Summarization steps ----------------------------------------------------


class PrepareLlamaModelStep(Step):
    name = "prepare_llama_model"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        marker = st.dirs["outputs"] / "llama_model_ready.json"
        target_model = ctx.settings.llm_model
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and str(payload.get("model", "")) == target_model:
                return True
        return False

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        marker = st.dirs["outputs"] / "llama_model_ready.json"
        target_model = ctx.settings.llm_model
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and str(payload.get("model", "")) == target_model:
                return True

        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="llama_model_progress",
            data={"status": "loading", "model": target_model},
        )
        st.logger.info("warming llama model: %s", target_model)
        try:
            st.logger.info("waiting for gpu slot: llama warmup")
            async with ctx.gpu_slot(st.task_id, st.user_id, "llm"):
                st.logger.info("gpu slot acquired: llama warmup")
                raw = await ctx.llm.chat_completion(
                    model=target_model,
                    system_prompt='Return compact JSON: {"status":"ready"}.',
                    user_prompt="Warm up model for upcoming summarization.",
                    timeout_seconds=1200,
                    max_tokens=32,
                    temperature=ctx.settings.llm_temperature,
                    top_p=ctx.settings.llm_top_p,
                    min_p=ctx.settings.llm_min_p,
                    repeat_penalty=ctx.settings.llm_repeat_penalty,
                    thinking=ctx.settings.llm_thinking,
                )
            log_payload(st.logger, "llama warmup response", raw)
        except Exception as exc:
            await ctx.bus.publish_event(
                user_id=st.user_id,
                task_id=str(st.task_id),
                event="llama_model_progress",
                data={"status": "failed", "model": target_model, "error": str(exc)},
            )
            raise

        parsed = parse_json_response(raw)
        write_json(
            marker,
            {
                "model": target_model,
                "ready_at": utcnow().isoformat(),
                "response": parsed,
            },
        )
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="llama_model_progress",
            data={"status": "ready", "model": target_model},
        )
        st.logger.info("llama model is ready: %s", target_model)
        return True


class PrepareSummaryChunksStep(Step):
    name = "prepare_summary_chunks"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        chunks_file = st.dirs["root"] / "summary" / "chunks.json"
        return chunks_file.exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        summary_dir = st.dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        chunks_file = summary_dir / "chunks.json"
        if chunks_file.exists():
            return True

        transcript_json = st.dirs["outputs"] / "transcript.json"
        if not transcript_json.exists():
            raise RuntimeError("Missing transcript for summarization")
        transcript = json.loads(transcript_json.read_text(encoding="utf-8")).get("text", "")
        if not isinstance(transcript, str) or not transcript.strip():
            st.logger.info("summary chunks skipped: empty transcript")
            write_json(chunks_file, {"chunks": [], "segmentation": "split"})
            write_json(st.dirs["outputs"] / "summary_chunks.json", {"chunks": [], "segmentation": "split"})
            return True

        st.logger.info("summary chunk preparation started")
        mode = str(getattr(ctx.settings, "summary_segmentation", "auto") or "auto")
        budget_cfg = token_budget_config(ctx.settings, await ctx.get_n_ctx(st.task_id, st.logger))
        timeout_seconds = int(getattr(ctx.settings, "llm_chat_timeout_seconds", 600))
        segment_prompt = render_prompt_with_language(
            load_prompt(ctx.settings.prompts_dir, "segment_prompt.md", _SEGMENT_PROMPT_FALLBACK),
            effective_language(st.task_options, st.dirs),
        )
        prompt_tokens = await ctx.llm.count_tokens(
            text=segment_prompt,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )
        transcript_tokens = await ctx.llm.count_tokens(
            text=transcript,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )

        send_whole = False
        if mode == "never":
            if not whole_transcript_possible(budget_cfg, prompt_tokens, transcript_tokens):
                raise RuntimeError(
                    f"summary segmentation=never: transcript (~{transcript_tokens} tokens)"
                    f" cannot fit the model context window (n_ctx={budget_cfg.n_ctx})"
                    " in one piece"
                )
            send_whole = True
        elif mode == "auto":
            send_whole = fits_whole_transcript(budget_cfg, prompt_tokens, transcript_tokens)

        if send_whole:
            chunks = [transcript]
            st.logger.info(
                "summary segmentation: whole transcript (mode=%s tokens=%d prompt=%d n_ctx=%d)",
                mode, transcript_tokens, prompt_tokens, budget_cfg.n_ctx,
            )
        else:
            window_tokens = derive_window_tokens(
                budget_cfg,
                prompt_tokens,
                cap=int(getattr(ctx.settings, "summary_segment_window_cap", 8192)),
            )
            st.logger.info(
                "summary segmentation: split (mode=%s tokens=%d window=%d n_ctx=%d)",
                mode, transcript_tokens, window_tokens, budget_cfg.n_ctx,
            )
            chunks = await ctx.llm.chunk_text(
                text=transcript,
                model=ctx.settings.llm_model,
                window_tokens=window_tokens,
                overlap_ratio=0.15,
                tokenizer_path=tokenizer_path(ctx.settings),
            )
        payload = {"chunks": chunks, "segmentation": "whole" if send_whole else "split"}
        st.logger.info("summary chunk preparation finished: %s windows", len(chunks))
        write_json(chunks_file, payload)
        write_json(st.dirs["outputs"] / "summary_chunks.json", payload)
        return True


class SummarizeWindowsStep(Step):
    name = "summarize_windows"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        output = st.dirs["root"] / "summary" / "windows.json"
        if not output.exists():
            return False
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        windows = payload.get("windows") if isinstance(payload, dict) else None
        return isinstance(windows, list)

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        summary_dir = st.dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        output = summary_dir / "windows.json"
        output_mirror = st.dirs["outputs"] / "window_summaries.json"

        output_language = effective_language(st.task_options, st.dirs)
        segment_prompt = render_prompt_with_language(
            load_prompt(ctx.settings.prompts_dir, "segment_prompt.md", _SEGMENT_PROMPT_FALLBACK),
            output_language,
        )
        chunks_file = summary_dir / "chunks.json"
        if not chunks_file.exists():
            chunks_file = st.dirs["outputs"] / "summary_chunks.json"
        if not chunks_file.exists():
            raise RuntimeError("Missing summary chunks")
        chunks_payload = json.loads(chunks_file.read_text(encoding="utf-8"))
        chunks = chunks_payload.get("chunks") if isinstance(chunks_payload, dict) else None
        if not isinstance(chunks, list):
            raise RuntimeError("Invalid summary chunks payload")
        whole_mode = chunks_payload.get("segmentation") == "whole"
        total_windows = len(chunks)

        windows_by_index: dict[int, dict[str, Any]] = {}
        if output.exists():
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            raw_windows = payload.get("windows") if isinstance(payload, dict) else None
            if isinstance(raw_windows, list):
                for item in raw_windows:
                    if not isinstance(item, dict):
                        continue
                    raw_index = item.get("window_index")
                    try:
                        idx = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    if idx < 1:
                        continue
                    summary_payload = item.get("summary")
                    path = item.get("path")
                    if not isinstance(path, str) or not path.strip():
                        path = str(summary_dir / f"window_{idx:02d}.txt")
                    windows_by_index[idx] = {
                        "window_index": idx,
                        "summary": summary_payload,
                        "path": path,
                    }

        file_pattern = re.compile(r"^window_(\d+)\.txt$")
        for window_path in sorted(summary_dir.glob("window_*.txt")):
            match = file_pattern.match(window_path.name)
            if not match:
                continue
            idx = int(match.group(1))
            if idx in windows_by_index:
                continue
            content = window_path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(content)
                summary: str | dict = parsed if isinstance(parsed, dict) else content
            except json.JSONDecodeError:
                summary = content
            windows_by_index[idx] = {
                "window_index": idx,
                "summary": summary,
                "path": str(window_path),
            }

        for idx in list(windows_by_index.keys()):
            if idx > total_windows:
                windows_by_index.pop(idx, None)

        restored = sum(1 for idx in windows_by_index if 1 <= idx <= total_windows)
        if restored:
            st.logger.info("restored summarized windows: %s/%s", restored, total_windows)
        if restored == total_windows:
            ordered = [windows_by_index[idx] for idx in sorted(windows_by_index)]
            write_json(output, {"windows": ordered})
            write_json(output_mirror, {"windows": ordered})
            redacted_path = st.dirs["outputs"] / "redacted_transcript.txt"
            redacted_path.write_text(
                "".join(str(w.get("summary", "")).rstrip("\n") + "\n\n" for w in ordered),
                encoding="utf-8",
            )
            st.logger.info("window summaries already complete: %s", total_windows)
            return True

        st.logger.info("window summarization started: %s windows", len(chunks))
        budget_cfg = token_budget_config(ctx.settings, await ctx.get_n_ctx(st.task_id, st.logger))
        total_parts = len(chunks) + 1
        # A whole-transcript rewrite generates output comparable to the input
        # size — that is final-stage territory, not a 2k-window call.
        timeout_seconds = int(
            getattr(ctx.settings, "llm_final_timeout_seconds", 1800)
            if whole_mode
            else getattr(ctx.settings, "llm_chat_timeout_seconds", 600)
        )
        redacted_path = st.dirs["outputs"] / "redacted_transcript.txt"
        redacted_path.write_text(
            "".join(
                str(windows_by_index[i].get("summary", "")).rstrip("\n") + "\n\n"
                for i in sorted(windows_by_index)
            ),
            encoding="utf-8",
        )
        while True:
            try:
                for idx, chunk in enumerate(chunks, start=1):
                    await ctx.check_paused(st.task_id)
                    if idx in windows_by_index:
                        st.logger.info("window %s/%s already summarized, skipping", idx, len(chunks))
                        await ctx.bus.publish_event(
                            user_id=st.user_id,
                            task_id=str(st.task_id),
                            event="summary_progress",
                            data={"current": idx, "total": total_parts},
                            throttle_key="summary_progress",
                        )
                        await ctx.persist_summary_progress(st.task_id, idx, total_parts)
                        continue
                    st.logger.info("summarizing window %s/%s", idx, len(chunks))

                    # Stage A: adaptive token budget
                    user_prompt = f"Window {idx}/{len(chunks)}\n\n{chunk}"
                    input_tokens = await ctx.llm.count_tokens(
                        text=user_prompt,
                        model=ctx.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=tokenizer_path(ctx.settings),
                    )
                    window_cfg = uncap_segment_for_input(budget_cfg, input_tokens)
                    target_tokens, min_out, max_out = compute_segment_budget(input_tokens, window_cfg)
                    budgeted_prompt = render_prompt_budget_vars(
                        segment_prompt,
                        input_tokens=input_tokens,
                        target_tokens=target_tokens,
                        target_ratio=window_cfg.segment_ratio,
                    )
                    st.logger.info(
                        "window %s/%s token_budget input=%d target=%d min=%d max=%d",
                        idx, len(chunks), input_tokens, target_tokens, min_out, max_out,
                    )
                    st.logger.info("waiting for gpu slot: summarize window %s/%s", idx, len(chunks))
                    _win_t_q0 = time.monotonic()
                    async with ctx.gpu_slot(st.task_id, st.user_id, "llm"):
                        _win_t_q_ms = round((time.monotonic() - _win_t_q0) * 1000)
                        st.logger.info("gpu slot acquired: summarize window %s/%s", idx, len(chunks))
                        _win_t0 = time.monotonic()
                        try:
                            raw = await ctx.llm.chat_completion(
                                model=ctx.settings.llm_model,
                                system_prompt=budgeted_prompt,
                                user_prompt=user_prompt,
                                timeout_seconds=timeout_seconds,
                                temperature=ctx.settings.llm_temperature,
                                top_p=ctx.settings.llm_top_p,
                                min_p=ctx.settings.llm_min_p,
                                repeat_penalty=ctx.settings.llm_repeat_penalty,
                                cache_prompt=True,
                                use_json_format=False,
                                thinking=ctx.settings.llm_thinking,
                                num_ctx=budget_cfg.n_ctx,
                            )
                        except RuntimeError as exc:
                            if whole_mode and is_context_overflow_error(str(exc)):
                                mode = str(getattr(ctx.settings, "summary_segmentation", "auto") or "auto")
                                if mode == "never":
                                    raise RuntimeError(
                                        "summary segmentation=never: the model cannot process"
                                        f" the transcript in one piece (n_ctx={budget_cfg.n_ctx}): {exc}"
                                    ) from exc
                                raise _WholeTranscriptOverflow() from exc
                            raise
                        _win_t_ms = round((time.monotonic() - _win_t0) * 1000)
                    actual_output_tokens = await ctx.llm.count_tokens(
                        text=raw,
                        model=ctx.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=tokenizer_path(ctx.settings),
                    )
                    log_metrics(st.logger, SummarizationMetrics(
                        stage_name="segment",
                        input_tokens=input_tokens,
                        target_tokens=target_tokens,
                        actual_output_tokens=actual_output_tokens,
                    ))
                    log_payload(st.logger, f"llm window response index={idx}", raw, max_chars=200)
                    _win_em = ctx.get_emitter(st.task_id)
                    if _win_em:
                        _n_ctx = budget_cfg.n_ctx
                        _win_em.emit({
                            "stage": "summarize.segment",
                            "status": "ok",
                            "segment_id": idx,
                            "t_wall_ms": _win_t_ms,
                            "t_queue_ms": _win_t_q_ms,
                            "llm_prompt_tokens": input_tokens,
                            "llm_completion_tokens": actual_output_tokens,
                            "llm_total_tokens": input_tokens + actual_output_tokens,
                            "llm_tok_per_s": round(actual_output_tokens / (_win_t_ms / 1000), 2) if _win_t_ms > 0 else None,
                            "llm_ctx_utilization": round(input_tokens / _n_ctx, 4) if _n_ctx > 0 else None,
                            "retries": 0,
                            **QualityAnalyzer(
                                shingle_n=ctx.settings.metrics_redundancy_shingle_n,
                                simhash_bits=ctx.settings.metrics_redundancy_simhash_bits,
                                max_hamming=ctx.settings.metrics_redundancy_max_hamming,
                            ).analyze(
                                summary_text=raw,
                                transcript_text=chunk,
                                prompt_tokens=input_tokens,
                                completion_tokens=actual_output_tokens,
                            ),
                        })
                    window_path = summary_dir / f"window_{idx:02d}.txt"
                    window_path.write_text(raw, encoding="utf-8")
                    windows_by_index[idx] = {"window_index": idx, "summary": raw, "path": str(window_path)}
                    ordered = [windows_by_index[item_idx] for item_idx in sorted(windows_by_index)]
                    write_json(output, {"windows": ordered})
                    write_json(output_mirror, {"windows": ordered})
                    redacted_path = st.dirs["outputs"] / "redacted_transcript.txt"
                    with redacted_path.open("a", encoding="utf-8") as rf:
                        rf.write(raw.rstrip("\n") + "\n\n")
                    await ctx.bus.publish_event(
                        user_id=st.user_id,
                        task_id=str(st.task_id),
                        event="segment_summary_text",
                        data={"index": idx, "total": total_windows, "text": raw},
                    )
                    await ctx.bus.publish_event(
                        user_id=st.user_id,
                        task_id=str(st.task_id),
                        event="summary_progress",
                        data={"current": idx, "total": total_parts},
                        throttle_key="summary_progress",
                    )
                    await ctx.persist_summary_progress(st.task_id, idx, total_parts)
            except _WholeTranscriptOverflow as overflow:
                st.logger.warning(
                    "whole-transcript rewrite exceeded the context window; "
                    "falling back to segmentation: %s",
                    overflow.__cause__,
                )
                prompt_tokens = await ctx.llm.count_tokens(
                    text=segment_prompt,
                    model=ctx.settings.llm_model,
                    timeout_seconds=int(getattr(ctx.settings, "llm_chat_timeout_seconds", 600)),
                    tokenizer_path=tokenizer_path(ctx.settings),
                )
                window_tokens = derive_window_tokens(
                    budget_cfg,
                    prompt_tokens,
                    cap=int(getattr(ctx.settings, "summary_segment_window_cap", 8192)),
                )
                chunks = await ctx.llm.chunk_text(
                    text=chunks[0],
                    model=ctx.settings.llm_model,
                    window_tokens=window_tokens,
                    overlap_ratio=0.15,
                    tokenizer_path=tokenizer_path(ctx.settings),
                )
                split_payload = {"chunks": chunks, "segmentation": "split"}
                write_json(summary_dir / "chunks.json", split_payload)
                write_json(st.dirs["outputs"] / "summary_chunks.json", split_payload)
                whole_mode = False
                windows_by_index = {}
                total_windows = len(chunks)
                total_parts = len(chunks) + 1
                timeout_seconds = int(getattr(ctx.settings, "llm_chat_timeout_seconds", 600))
                redacted_path.write_text("", encoding="utf-8")
                await ctx.bus.publish_event(
                    user_id=st.user_id,
                    task_id=str(st.task_id),
                    event="summary_progress",
                    data={"current": 0, "total": total_parts},
                    throttle_key="summary_progress",
                )
                await ctx.persist_summary_progress(st.task_id, 0, total_parts)
                st.logger.info("fallback segmentation: %s windows", len(chunks))
                continue
            break
        ordered = [windows_by_index[idx] for idx in sorted(windows_by_index)]
        write_json(output, {"windows": ordered})
        write_json(output_mirror, {"windows": ordered})
        st.logger.info("window summaries generated: %s", len(ordered))
        return True


class PackWindowNotesStep(Step):
    """Stage B — pack/dedup window notes so they fit in the final context budget.

    If prompt + notes + estimated output + safety margin already fit within n_ctx,
    the step is a no-op (writes the passthrough marker and exits).  Otherwise it
    compresses notes in batches until they fit.
    """

    name = "pack_window_notes"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        packed_file = st.dirs["root"] / "summary" / "packed_notes.json"
        return packed_file.exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        summary_dir = st.dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        packed_file = summary_dir / "packed_notes.json"
        if packed_file.exists():
            return True

        # Load window notes
        windows_file = summary_dir / "windows.json"
        if not windows_file.exists():
            windows_file = st.dirs["outputs"] / "window_summaries.json"
        if not windows_file.exists():
            raise RuntimeError("Missing window summaries for packing step")
        windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
        if not isinstance(windows, list):
            raise RuntimeError("Invalid window summaries payload")

        output_language = effective_language(st.task_options, st.dirs)
        timeout_seconds = int(getattr(ctx.settings, "llm_final_timeout_seconds", 1800))
        budget_cfg = token_budget_config(ctx.settings, await ctx.get_n_ctx(st.task_id, st.logger))

        # Load final prompt to measure its token cost
        final_prompt_text = render_prompt_budget_vars(
            render_prompt_with_language(
                load_prompt(
                    ctx.settings.prompts_dir,
                    "global_prompt.md",
                    "Produce a structured knowledge document from the notes.",
                ),
                output_language,
            ),
        )
        final_prompt_tokens = await ctx.llm.count_tokens(
            text=final_prompt_text,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )
        st.logger.info(
            "pack_window_notes: final_prompt_tokens=%d",
            final_prompt_tokens,
        )

        # Count total tokens of all notes
        notes_texts: list[str] = [extract_window_text(w) for w in windows]
        note_token_counts: list[int] = []
        for text in notes_texts:
            tc = await ctx.llm.count_tokens(
                text=text,
                model=ctx.settings.llm_model,
                timeout_seconds=timeout_seconds,
                tokenizer_path=tokenizer_path(ctx.settings),
            )
            note_token_counts.append(tc)
        total_notes_tokens = sum(note_token_counts)

        packing_triggered = not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens)
        st.logger.info(
            "pack_window_notes: total_notes_tokens=%d packing_needed=%s",
            total_notes_tokens,
            packing_triggered,
        )

        packing_pass_count = 0

        if packing_triggered:
            pack_prompt_template = render_prompt_with_language(
                load_prompt(
                    ctx.settings.prompts_dir,
                    "pack_prompt.md",
                    "Integrate and deduplicate the following notes. "
                    "Target output: ~${TARGET_WORDS} words (~${TARGET_RATIO}% of input, input: ~${INPUT_WORDS} words).\n"
                    "Output language: ${LANG}.",
                ),
                output_language,
            )

            current_texts = notes_texts
            current_token_counts = note_token_counts

            while not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens) and len(current_texts) > 0:
                packing_pass_count += 1
                st.logger.info(
                    "packing pass %d: total_tokens=%d notes=%d",
                    packing_pass_count,
                    total_notes_tokens,
                    len(current_texts),
                )

                # Split notes into batches not exceeding pack_batch_max_input_tokens
                batches: list[list[str]] = []
                current_batch: list[str] = []
                current_batch_tokens = 0
                for note_text, note_tc in zip(current_texts, current_token_counts):
                    if (
                        current_batch
                        and current_batch_tokens + note_tc > budget_cfg.pack_batch_max_input_tokens
                    ):
                        batches.append(current_batch)
                        current_batch = []
                        current_batch_tokens = 0
                    current_batch.append(note_text)
                    current_batch_tokens += note_tc
                if current_batch:
                    batches.append(current_batch)

                new_texts: list[str] = []
                new_token_counts: list[int] = []
                for b_idx, batch in enumerate(batches, 1):
                    await ctx.check_paused(st.task_id)
                    batch_input = "\n\n".join(batch)
                    batch_input_tokens = await ctx.llm.count_tokens(
                        text=batch_input,
                        model=ctx.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=tokenizer_path(ctx.settings),
                    )
                    target_tokens, min_out, max_out = compute_pack_budget(
                        batch_input_tokens, budget_cfg
                    )
                    pack_system_prompt = render_prompt_budget_vars(
                        pack_prompt_template,
                        input_tokens=batch_input_tokens,
                        target_tokens=target_tokens,
                        target_ratio=budget_cfg.pack_ratio,
                    )
                    st.logger.info(
                        "pack batch %d/%d: input=%d target=%d min=%d max=%d",
                        b_idx, len(batches), batch_input_tokens, target_tokens, min_out, max_out,
                    )
                    async with ctx.gpu_slot(st.task_id, st.user_id, "llm"):
                        packed_text = await ctx.llm.chat_completion(
                            model=ctx.settings.llm_model,
                            system_prompt=pack_system_prompt,
                            user_prompt=batch_input,
                            timeout_seconds=timeout_seconds,
                            temperature=ctx.settings.llm_temperature,
                            top_p=ctx.settings.llm_top_p,
                            min_p=ctx.settings.llm_min_p,
                            repeat_penalty=ctx.settings.llm_repeat_penalty,
                            cache_prompt=True,
                            use_json_format=False,
                            thinking=ctx.settings.llm_thinking,
                            num_ctx=budget_cfg.n_ctx,
                        )
                    packed_tc = await ctx.llm.count_tokens(
                        text=packed_text,
                        model=ctx.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=tokenizer_path(ctx.settings),
                    )
                    log_metrics(st.logger, SummarizationMetrics(
                        stage_name="pack",
                        input_tokens=batch_input_tokens,
                        target_tokens=target_tokens,
                        actual_output_tokens=packed_tc,
                        packing_triggered=True,
                        packing_pass_count=packing_pass_count,
                    ))
                    new_texts.append(packed_text)
                    new_token_counts.append(packed_tc)

                current_texts = new_texts
                current_token_counts = new_token_counts
                total_notes_tokens = sum(current_token_counts)

                # Guard: stop if packing produced a single note and still doesn't fit
                if len(current_texts) == 1 and not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens):
                    st.logger.warning(
                        "packing converged to a single note but still exceeds budget "
                        "(%d tokens); proceeding anyway",
                        total_notes_tokens,
                    )
                    break

            notes_texts = current_texts

        write_json(
            packed_file,
            {
                "notes": notes_texts,
                "packing_triggered": packing_triggered,
                "packing_pass_count": packing_pass_count,
                "total_notes_tokens": total_notes_tokens,
            },
        )
        st.logger.info(
            "pack_window_notes complete: notes=%d total_tokens=%d packing_triggered=%s passes=%d",
            len(notes_texts),
            total_notes_tokens,
            packing_triggered,
            packing_pass_count,
        )
        return True


class FinalizePromptStep(Step):
    """Stage C — render the final document for one selected prompt.

    Created by ``resolve_step`` (not in STEP_REGISTRY): ``summarize_final`` maps
    to ``FinalizePromptStep(source="system", id="summary")`` and ``finalize:*``
    to the parsed source/id. The prompt text and display name are resolved via a
    PromptSource strategy (system vs. user).
    """

    name = "finalize"
    lane = None

    def __init__(self, source: str, id: str) -> None:
        self.source = source
        self.id = id

    def _paths(self, st: StepState) -> tuple[bool, Path, Path, Path]:
        is_summary = self.source == "system" and self.id == "summary"
        summary_dir = st.dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        if is_summary:
            summary_json = summary_dir / "final.json"
            summary_md = summary_dir / "final.md"
        else:
            results_dir = summary_dir / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            summary_json = results_dir / f"{self.source}__{self.id}.json"
            summary_md = results_dir / f"{self.source}__{self.id}.md"
        return is_summary, summary_dir, summary_json, summary_md

    def _validate_id(self) -> None:
        # Defense-in-depth: validate the id BEFORE it is used to build any result
        # path. A user-source id must be a UUID; this rejects path-traversal ids
        # (e.g. "../../etc/passwd") regardless of downstream call ordering.
        if self.source == "user":
            try:
                uuid.UUID(self.id)
            except (ValueError, TypeError):
                raise RuntimeError(f"invalid user prompt id: {self.id!r}")

    async def _restore_if_present(self, ctx: "PipelineContext", st: StepState) -> bool:
        """Validate the id, and if the result files already exist, re-index them
        (summary_path for the system summary, prompt_results otherwise) and report
        completion. Shared by already_done (resume probe) and run (re-entry guard)
        so the pre-flight stays single-sourced."""
        self._validate_id()
        is_summary, _summary_dir, summary_json, summary_md = self._paths(st)
        if not (summary_json.exists() and summary_md.exists()):
            return False
        if is_summary:
            async with ctx.session_factory() as session:
                repo = Repo(session)
                task = await repo.get_task_by_id(st.task_id)
                if task is None:
                    raise RuntimeError("task not found during final summary restore")
                summary_path = str(summary_md)
                if task.summary_path != summary_path:
                    task.summary_path = summary_path
                    await session.commit()
        else:
            name = await prompt_source_for(self.source).display_name(ctx, self.id, st.user_id)
            await ctx.persist_prompt_result(st.task_id, self.source, self.id, name, str(summary_md))
        return True

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return await self._restore_if_present(ctx, st)

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        source = self.source
        id = self.id
        if await self._restore_if_present(ctx, st):
            return True

        # id already validated in _restore_if_present; reuse the same path layout
        # for the fresh summarization below.
        is_summary, summary_dir, summary_json, summary_md = self._paths(st)
        output_language = effective_language(st.task_options, st.dirs)
        timeout_seconds = int(getattr(ctx.settings, "llm_final_timeout_seconds", 1800))
        budget_cfg = token_budget_config(ctx.settings, await ctx.get_n_ctx(st.task_id, st.logger))

        # Load packed notes if the packing step ran, else fall back to window summaries.
        # fallback_windows: list passed to _summarize_hierarchical if flat call fails.
        # merged: the user_prompt for the flat final call.
        packed_file = summary_dir / "packed_notes.json"
        if packed_file.exists():
            packed_payload = json.loads(packed_file.read_text(encoding="utf-8"))
            packed_notes: list[str] = packed_payload.get("notes", [])
            if not isinstance(packed_notes, list):
                packed_notes = []
            packing_triggered: bool = bool(packed_payload.get("packing_triggered", False))
            packing_pass_count: int = int(packed_payload.get("packing_pass_count", 0))
            merged = "\n\n".join(packed_notes)
            total_windows = len(packed_notes)
            total_parts = total_windows + 1
            st.logger.info(
                "final summary: using packed notes (%d) packing_triggered=%s",
                len(packed_notes),
                packing_triggered,
            )
        else:
            windows_file = summary_dir / "windows.json"
            if not windows_file.exists():
                windows_file = st.dirs["outputs"] / "window_summaries.json"
            if not windows_file.exists():
                raise RuntimeError("Missing window summaries")
            windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
            if not isinstance(windows, list):
                raise RuntimeError("Invalid window summaries payload")
            # Build merged with [Segment N] prefix (same as original behaviour)
            parts: list[str] = []
            for w in windows:
                idx = w.get("window_index", "?")
                text = extract_window_text(w)
                parts.append(f"[Segment {idx}]\n{text}" if text else f"[Segment {idx}]")
            merged = "\n\n".join(parts)
            packing_triggered = False
            packing_pass_count = 0
            total_windows = len(windows)
            total_parts = total_windows + 1

        st.logger.info("final summary generation started: notes=%s", total_windows)
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="summary_progress",
            data={"current": total_windows, "total": total_parts},
        )
        await ctx.persist_summary_progress(st.task_id, total_windows, total_parts)

        global_prompt_base = await prompt_source_for(source).load_text(
            ctx, id, output_language, st.user_id
        )
        # Stage C: adaptive token budget
        input_tokens = await ctx.llm.count_tokens(
            text=merged,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )
        target_tokens, min_out, max_out = compute_final_budget(input_tokens, budget_cfg)
        final_prompt_tokens = await ctx.llm.count_tokens(
            text=global_prompt_base,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )
        global_prompt = render_prompt_budget_vars(
            global_prompt_base,
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            target_ratio=budget_cfg.final_ratio,
        )
        st.logger.info(
            "final summary token_budget input=%d target=%d min=%d max=%d",
            input_tokens, target_tokens, min_out, max_out,
        )
        st.logger.info(
            "waiting for gpu slot: final summary (notes=%s payload_bytes=%s)",
            total_windows,
            len(merged.encode("utf-8")),
        )
        _fin_t_q0 = time.monotonic()
        async with ctx.gpu_slot(st.task_id, st.user_id, "llm"):
            _fin_t_q_ms = round((time.monotonic() - _fin_t_q0) * 1000)
            st.logger.info("gpu slot acquired: final summary")
            _fin_t0 = time.monotonic()
            raw = await ctx.llm.chat_completion(
                model=ctx.settings.llm_model,
                system_prompt=global_prompt,
                user_prompt=merged,
                timeout_seconds=timeout_seconds,
                temperature=ctx.settings.llm_temperature,
                top_p=ctx.settings.llm_top_p,
                min_p=ctx.settings.llm_min_p,
                repeat_penalty=ctx.settings.llm_repeat_penalty,
                use_json_format=False,
                thinking=ctx.settings.llm_thinking,
                num_ctx=budget_cfg.n_ctx,
            )
            _fin_t_ms = round((time.monotonic() - _fin_t0) * 1000)

        actual_output_tokens = await ctx.llm.count_tokens(
            text=raw,
            model=ctx.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path(ctx.settings),
        )
        log_metrics(st.logger, SummarizationMetrics(
            stage_name="final",
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            actual_output_tokens=actual_output_tokens,
            packing_triggered=packing_triggered,
            packing_pass_count=packing_pass_count,
        ))
        log_payload(st.logger, "llm final summary response", raw, max_chars=200)
        _fin_em = ctx.get_emitter(st.task_id)
        if _fin_em:
            _n_ctx = budget_cfg.n_ctx
            # Load transcript text for mismatch comparison
            _transcript_text = ""
            _transcript_json = st.dirs["outputs"] / "transcript.json"
            if _transcript_json.exists():
                try:
                    _transcript_text = json.loads(_transcript_json.read_text(encoding="utf-8")).get("text", "")
                except Exception:
                    pass
            _fin_em.emit({
                "stage": "summarize.global",
                "status": "ok",
                "t_wall_ms": _fin_t_ms,
                "t_queue_ms": _fin_t_q_ms,
                "llm_prompt_tokens": input_tokens,
                "llm_completion_tokens": actual_output_tokens,
                "llm_total_tokens": input_tokens + actual_output_tokens,
                "llm_tok_per_s": round(actual_output_tokens / (_fin_t_ms / 1000), 2) if _fin_t_ms > 0 else None,
                "llm_ctx_utilization": round(input_tokens / _n_ctx, 4) if _n_ctx > 0 else None,
                "packing_triggered": packing_triggered,
                "packing_pass_count": packing_pass_count,
                "retries": 0,
                **QualityAnalyzer(
                    shingle_n=ctx.settings.metrics_redundancy_shingle_n,
                    simhash_bits=ctx.settings.metrics_redundancy_simhash_bits,
                    max_hamming=ctx.settings.metrics_redundancy_max_hamming,
                ).analyze(
                    summary_text=raw,
                    transcript_text=_transcript_text or merged,
                    prompt_tokens=input_tokens,
                    completion_tokens=actual_output_tokens,
                ),
            })
        write_json(summary_json, {"raw": raw})
        summary_md.write_text(raw, encoding="utf-8")
        if is_summary:
            # Back-compat: the canonical summary mirrors into outputs/summary.*.
            write_json(st.dirs["outputs"] / "summary.json", {"raw": raw})
            (st.dirs["outputs"] / "summary.md").write_text(raw, encoding="utf-8")
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="summary_progress",
            data={"current": total_parts, "total": total_parts},
        )
        await ctx.persist_summary_progress(st.task_id, total_parts, total_parts)
        st.logger.info("final summary generated")

        if is_summary:
            async with ctx.session_factory() as session:
                repo = Repo(session)
                task = await repo.get_task_by_id(st.task_id)
                if task is None:
                    raise RuntimeError("task not found during final summary")
                task.summary_path = str(summary_md)
                await session.commit()
        name = await prompt_source_for(source).display_name(ctx, id, st.user_id)
        await ctx.persist_prompt_result(st.task_id, source, id, name, str(summary_md))
        return True
