from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vts.db.repo import Repo
from vts.services.diarization.merge import (
    SENTENCE_SPLIT_RE,
    drop_marginal_speakers,
    label_map,
    merge_entries,
    render_cleaned_transcript,
    speaker_label_word,
    trim_repetitive_entries,
    trim_repetitive_units,
)
from vts.services.storage import write_json
from vts.pipeline.steps.base import Step, StepState, log_payload

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# --- ASR domain helpers (pure module functions) -----------------------------


def normalize_language(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def effective_language(task_options: dict[str, Any], dirs: dict[str, Path]) -> str | None:
    explicit = normalize_language(task_options.get("language"))
    if explicit:
        return explicit
    detected = normalize_language(task_options.get("detected_language"))
    if detected:
        return detected
    marker = dirs["outputs"] / "language_detection.json"
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_language(payload.get("language"))


def normalize_token(value: str) -> str:
    return re.sub(r"[^\wа-яА-ЯёЁ]+", "", value, flags=re.UNICODE).strip().lower()


def is_probable_asr_hallucination(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return False
    tokens = [normalize_token(token) for token in re.split(r"\s+", normalized)]
    tokens = [token for token in tokens if token]
    if len(tokens) < 10:
        return False

    token_counts = Counter(tokens)
    unique_ratio = len(token_counts) / float(len(tokens))
    top_token_ratio = token_counts.most_common(1)[0][1] / float(len(tokens))

    sentences = [normalize_token(part) for part in re.split(r"[.!?…]+", normalized)]
    sentences = [sentence for sentence in sentences if sentence]
    repeated_edge = False
    if len(sentences) >= 5:
        head = sentences[0]
        tail = sentences[-1]
        head_repeats = 0
        tail_repeats = 0
        for sentence in sentences:
            if sentence == head:
                head_repeats += 1
            else:
                break
        for sentence in reversed(sentences):
            if sentence == tail:
                tail_repeats += 1
            else:
                break
        repeated_edge = max(head_repeats, tail_repeats) >= 5

    signals = 0
    if unique_ratio < 0.30:
        signals += 1
    if top_token_ratio > 0.35:
        signals += 1
    if repeated_edge:
        signals += 1
    return signals >= 2


def transcript_quality_score(text: str) -> float:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return 0.0
    tokens = [normalize_token(token) for token in re.split(r"\s+", normalized)]
    tokens = [token for token in tokens if token]
    if not tokens:
        return 0.0
    unique_ratio = len(set(tokens)) / float(len(tokens))
    return len(tokens) * unique_ratio


def trim_repetitive_edges(text: str) -> tuple[str, dict[str, Any]]:
    if not text.strip():
        return "", {
            "removed_head_sentences": 0,
            "removed_tail_sentences": 0,
            "head_phrase": None,
            "tail_phrase": None,
        }
    raw_sentences = [item.strip() for item in SENTENCE_SPLIT_RE.split(text) if item.strip()]
    if not raw_sentences:
        return text.strip(), {
            "removed_head_sentences": 0,
            "removed_tail_sentences": 0,
            "head_phrase": None,
            "tail_phrase": None,
        }

    # Repeat-detection core lives in merge.py (trim_repetitive_units) so the
    # diarized path (trim_repetitive_entries, operating on whole entries
    # instead of sentences) shares the exact same heuristic rather than
    # reimplementing it and risking drift between the two paths.
    sentences, meta = trim_repetitive_units(raw_sentences)
    cleaned = " ".join(sentences).strip()
    if not cleaned:
        cleaned = text.strip()
    return cleaned, meta


def tail_prompt(text: str, max_chars: int = 800) -> str | None:
    normalized = text.strip()
    if not normalized:
        return None
    return normalized[-max_chars:]


def apply_diarization(
    entries: list[dict[str, Any]],
    raw_json_by_index: dict[int, dict[str, Any]],
    diarization_path: Path,
    *,
    min_words: int,
    min_seconds: float,
    min_share: float,
    language: str | None = "ru",
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """Attribute entries to speakers, returning the rendered text when diarized.

    Returns the entries unchanged, `None` text and `None` cleanup meta when
    there is no diarization artifact or it cannot be read: the transcript is
    the valuable output, and a broken speaker artifact must never fail a task.

    When diarized, hallucination cleanup runs on the ENTRY list before
    rendering — never on the rendered dialogue text, which already carries
    "<label> N:" labels that the sentence-splitting heuristic would corrupt.
    `drop_marginal_speakers` runs exactly once here, and both the returned
    entries and the rendered text derive from that same cleaned list, so a
    downstream consumer (e.g. speaker enrollment) never sees a phantom speaker
    that the rendered text has already folded away.

    `language` selects the label word ("Голос" for ru, "Speaker" otherwise —
    see speaker_label_word) so it matches the recording's language, which is
    also what segment_prompt.md's output-language instruction targets. This is
    a render-time choice only: entries[i]["speaker"] keeps the technical
    SPEAKER_00 tag regardless of language.

    The "ru" default applies only when the argument is OMITTED — it exists so
    callers and tests predating per-language labels keep their old output. It
    is not the behaviour for language=None: an explicit None resolves to
    "Speaker" via speaker_label_word. The real caller (MergeTranscriptStep)
    always passes effective_language() explicitly, and cannot pass None in
    practice — DetectLanguageStep raises on every no-language path and runs
    before merge_transcript in the DAG, so a task with no established language
    fails before reaching here.
    """
    if not diarization_path.exists():
        return entries, None, None
    try:
        payload = json.loads(diarization_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return entries, None, None
    if not isinstance(payload, dict):
        return entries, None, None

    diar_segments = payload.get("segments")
    if not isinstance(diar_segments, list) or not diar_segments:
        return entries, None, None

    merged = merge_entries(entries, raw_json_by_index, diar_segments, min_words, min_seconds)
    trimmed, cleanup_meta = trim_repetitive_entries(merged)
    cleaned = drop_marginal_speakers(trimmed, min_share)
    mapping = label_map(cleaned, speaker_label_word(language))
    text = render_cleaned_transcript(cleaned, mapping)
    return cleaned, text, cleanup_meta


def transcribe_audio_path(dirs: dict[str, Path]) -> Path:
    trimmed = dirs["media"] / "audio_16k_trimmed.wav"
    if trimmed.exists():
        return trimmed
    return dirs["media"] / "audio_16k.wav"


# --- Transcription steps ----------------------------------------------------


class DetectLanguageStep(Step):
    name = "detect_language"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        # Mirrors the legacy dry_run=True path: reports whether the language is
        # already known, and (as in the original) records an explicit
        # task-option language into task_options as a side effect.
        already = normalize_language(st.task_options.get("detected_language"))
        if already:
            return True
        explicit = normalize_language(st.task_options.get("language"))
        if explicit:
            st.task_options["detected_language"] = explicit
            return True
        return False

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        marker = st.dirs["outputs"] / "language_detection.json"

        # Already determined (persisted to DB on a previous run, loaded into task_options at startup).
        already = normalize_language(st.task_options.get("detected_language"))
        if already:
            return True

        explicit = normalize_language(st.task_options.get("language"))
        if explicit:
            st.task_options["detected_language"] = explicit
            write_json(
                marker,
                {
                    "source": "task_option",
                    "language": explicit,
                    "confidence": 1.0,
                    "detected_at": utcnow().isoformat(),
                },
            )
            await ctx.persist_detected_language(st.task_id, explicit, 1.0)
            return True

        manifest_path = st.dirs["outputs"] / "segments_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Missing segment manifest for language detection")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        specs = payload.get("segments", [])
        if not isinstance(specs, list) or not specs:
            raise RuntimeError("Missing segment files for language detection")
        first = specs[0]
        segment_path = st.dirs["segments"] / str(first.get("file", ""))
        if not segment_path.exists():
            raise RuntimeError("Missing first segment for language detection")

        st.logger.info("waiting for gpu slot: detect language")
        async with ctx.gpu_slot(st.task_id, st.user_id, "asr"):
            st.logger.info("gpu slot acquired: detect language")
            raw = await ctx.whisper.detect_language(audio_path=segment_path)
        log_payload(st.logger, "detect_language response", raw)
        language = normalize_language(raw.get("language"))
        _conf_raw = raw.get("language_probability")
        confidence: float | None = float(_conf_raw) if isinstance(_conf_raw, (int, float)) else None
        threshold = ctx.settings.language_detection_confidence_threshold
        if not language:
            raise RuntimeError("Auto language detection failed: language not recognized")
        if confidence is None:
            raise RuntimeError(
                f"Auto language detection failed: language_probability missing for language={language}"
            )
        if confidence < threshold:
            raise RuntimeError(
                f"Auto language detection confidence too low: language={language}, "
                f"confidence={confidence}, threshold={threshold}"
            )
        st.task_options["detected_language"] = language
        write_json(
            marker,
            {
                "source": "whisper_first_segment",
                "language": language,
                "confidence": confidence,
                "threshold": threshold,
                "detected_at": utcnow().isoformat(),
            },
        )
        await ctx.persist_detected_language(st.task_id, language, confidence)
        st.logger.info("language detected: %s (confidence=%.3f)", language, confidence)
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="phase",
            data={
                "phase": "detect_language",
                "status": "done",
                "language": language,
                "confidence": confidence,
            },
        )
        return True


class TranscribeSegmentsStep(Step):
    name = "transcribe_segments"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        manifest_path = st.dirs["outputs"] / "segments_manifest.json"
        if not manifest_path.exists():
            # Transcript exists means transcription was already merged.
            transcript_json = st.dirs["outputs"] / "transcript.json"
            return transcript_json.exists()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        specs: list[dict[str, Any]] = payload.get("segments", [])
        if not specs:
            return True

        async with ctx.session_factory() as session:
            repo = Repo(session)
            existing_segments = {seg.segment_index: seg for seg in await repo.get_task_segments(st.task_id)}
        missing = []
        for spec in specs:
            idx = int(spec["segment_index"])
            seg = existing_segments.get(idx)
            if seg is None:
                missing.append(spec)
                continue
            if isinstance(seg.raw_json, dict) and seg.raw_json:
                continue
            missing.append(spec)
        if not missing:
            return True
        return False

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        manifest_path = st.dirs["outputs"] / "segments_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Missing segment manifest")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        specs: list[dict[str, Any]] = payload.get("segments", [])
        if not specs:
            return False

        async with ctx.session_factory() as session:
            repo = Repo(session)
            existing_segments = {seg.segment_index: seg for seg in await repo.get_task_segments(st.task_id)}
        missing = []
        for spec in specs:
            idx = int(spec["segment_index"])
            seg = existing_segments.get(idx)
            if seg is None:
                missing.append(spec)
                continue
            if isinstance(seg.raw_json, dict) and seg.raw_json:
                continue
            missing.append(spec)
        if not missing:
            return True

        language = effective_language(st.task_options, st.dirs)
        if not language:
            raise RuntimeError("Missing transcription language after detection")
        missing.sort(key=lambda spec: int(spec["segment_index"]))
        text_by_index = {seg.segment_index: seg.text for seg in existing_segments.values() if seg.text.strip()}
        suspicious_by_index: dict[int, bool] = {
            seg.segment_index: is_probable_asr_hallucination(seg.text)
            for seg in existing_segments.values()
            if seg.text.strip()
        }
        transcript_txt = st.dirs["outputs"] / "transcript.txt"
        _need_set_transcript_path = not transcript_txt.exists()
        for spec in missing:
            await ctx.check_paused(st.task_id)
            idx = int(spec["segment_index"])
            segment_path = st.dirs["segments"] / str(spec["file"])
            start = float(spec["start"])
            end = float(spec["end"])
            initial_prompt = None
            if not suspicious_by_index.get(idx - 1, False):
                initial_prompt = tail_prompt(text_by_index.get(idx - 1, ""))
            st.logger.info("waiting for gpu slot: transcribe segment %s", idx)
            _asr_retries = 0
            _t_asr_q0 = time.monotonic()
            async with ctx.gpu_slot(st.task_id, st.user_id, "asr"):
                _t_asr_q_ms = round((time.monotonic() - _t_asr_q0) * 1000)
                st.logger.info("gpu slot acquired: transcribe segment %s", idx)
                _t_asr0 = time.monotonic()
                raw = await ctx.whisper.transcribe(
                    audio_path=segment_path,
                    language=language,
                    initial_prompt=initial_prompt,
                )
                _t_asr_ms = round((time.monotonic() - _t_asr0) * 1000)
            log_payload(st.logger, f"asr response segment={idx}", raw, max_chars=200)
            text = ctx.whisper.normalize_output(raw)
            suspicious = is_probable_asr_hallucination(text)
            if suspicious:
                _asr_retries = 1
                st.logger.warning("asr segment %s appears repetitive/noisy; retrying without tail prompt", idx)
                st.logger.info("waiting for gpu slot: transcribe segment %s retry", idx)
                async with ctx.gpu_slot(st.task_id, st.user_id, "asr"):
                    st.logger.info("gpu slot acquired: transcribe segment %s retry", idx)
                    retry_raw = await ctx.whisper.transcribe(
                        audio_path=segment_path,
                        language=language,
                        initial_prompt=None,
                    )
                retry_text = ctx.whisper.normalize_output(retry_raw)
                retry_suspicious = is_probable_asr_hallucination(retry_text)
                old_score = transcript_quality_score(text)
                new_score = transcript_quality_score(retry_text)
                if (not retry_suspicious) or (new_score > old_score):
                    raw = retry_raw
                    text = retry_text
                    suspicious = retry_suspicious
                    st.logger.info(
                        "asr retry accepted for segment %s (old_score=%.3f, new_score=%.3f)",
                        idx,
                        old_score,
                        new_score,
                    )
                else:
                    st.logger.info(
                        "asr retry rejected for segment %s (old_score=%.3f, new_score=%.3f)",
                        idx,
                        old_score,
                        new_score,
                    )
            suspicious_by_index[idx] = suspicious
            _asr_dur_s = end - start
            _asr_rtf = (_t_asr_ms / 1000.0) / _asr_dur_s if _asr_dur_s > 0 else None
            _asr_em = ctx.get_emitter(st.task_id)
            if _asr_em:
                _asr_em.emit({
                    "stage": "transcribe.segment",
                    "status": "ok",
                    "segment_id": idx,
                    "audio_start_s": round(start, 3),
                    "audio_end_s": round(end, 3),
                    "audio_duration_s": round(_asr_dur_s, 3),
                    "t_wall_ms": _t_asr_ms,
                    "t_queue_ms": _t_asr_q_ms,
                    "rtf": round(_asr_rtf, 4) if _asr_rtf is not None else None,
                    "retries": _asr_retries,
                    "whisper_backend": ctx.whisper.backend_name,
                    "artifacts": {"segment_file": str(spec.get("file", ""))},
                })
            text_by_index[idx] = text
            await ctx.bus.publish_event(
                user_id=st.user_id,
                task_id=str(st.task_id),
                event="transcribe_progress",
                data={"segment_index": idx, "total": len(specs)},
                throttle_key="transcribe_progress",
            )
            with transcript_txt.open("a", encoding="utf-8") as tf:
                tf.write(text.strip() + " ")
            async with ctx.session_factory() as session:
                repo = Repo(session)
                seg = await repo.upsert_asr_segment_payload(
                    task_id=st.task_id,
                    segment_index=idx,
                    start_sec=start,
                    end_sec=end,
                    text=text,
                    raw_json=raw,
                )
                if _need_set_transcript_path:
                    task_row = await repo.get_task_by_id(st.task_id)
                    if task_row is not None:
                        task_row.transcript_path = str(transcript_txt)
                    _need_set_transcript_path = False
                await session.commit()
            await ctx.bus.publish_event(
                user_id=st.user_id,
                task_id=str(st.task_id),
                event="transcript_segment_text",
                data={"index": idx, "total": len(specs), "text": text.strip()},
            )
            await asyncio.sleep(ctx.settings.services_database_write_throttle_ms / 1000.0)

        async with ctx.session_factory() as session:
            repo = Repo(session)
            all_segments = await repo.get_task_segments(st.task_id)
        asr_dir = st.dirs["root"] / "asr"
        asr_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            asr_dir / "segments_raw.json",
            {
                "segments": [
                    {
                        "segment_index": int(seg.segment_index),
                        "start": float(seg.start_sec),
                        "end": float(seg.end_sec),
                        "raw_json": seg.raw_json,
                    }
                    for seg in all_segments
                    if isinstance(seg.raw_json, dict) and bool(seg.raw_json)
                ]
            },
        )
        return True


class MergeTranscriptStep(Step):
    name = "merge_transcript"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        transcript_json = st.dirs["outputs"] / "transcript.json"
        transcript_txt = st.dirs["outputs"] / "transcript.txt"
        return transcript_json.exists() and transcript_txt.exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        transcript_json = st.dirs["outputs"] / "transcript.json"
        transcript_txt = st.dirs["outputs"] / "transcript.txt"
        if transcript_json.exists() and transcript_txt.exists():
            return True

        async with ctx.session_factory() as session:
            repo = Repo(session)
            segments = await repo.get_task_segments(st.task_id)
            entries: list[dict[str, Any]] = []
            merged_tokens: list[str] = []
            raw_json_by_index: dict[int, dict[str, Any]] = {}
            for segment in segments:
                text = segment.text.strip()
                if text:
                    merged_tokens.append(text)
                    if isinstance(segment.raw_json, dict) and segment.raw_json:
                        raw_json_by_index[len(entries)] = segment.raw_json
                    entries.append({"start": segment.start_sec, "end": segment.end_sec, "text": text})
            merged_text = " ".join(merged_tokens).strip()
            cleaned_text, cleanup_meta = trim_repetitive_edges(merged_text)

            entries, diarized_text, diarized_cleanup_meta = apply_diarization(
                entries,
                raw_json_by_index,
                st.dirs["outputs"] / "diarization.json",
                min_words=int(getattr(ctx.settings, "diarization_min_words", 2)),
                min_seconds=float(getattr(ctx.settings, "diarization_min_seconds", 0.8)),
                min_share=float(getattr(ctx.settings, "diarization_min_speaker_share", 0.05)),
                # The label word must match the recording's language: this is
                # the same effective_language() the ASR step used to transcribe,
                # and the same value segment_prompt.md's ${LANG} instruction
                # will later target — keeping them in lockstep is what fixes
                # the "output MUST be English" vs "keep Голос 1:" contradiction.
                language=effective_language(st.task_options, st.dirs),
            )
            # The diarized path cleans hallucinations at the entry level (see
            # apply_diarization), which can drop a different number of units than
            # the flat trim_repetitive_edges(merged_text) above ran on the same
            # underlying text. cleanup_meta must describe what actually landed in
            # `final_text`/`entries`, so swap it in whenever diarization applied.
            if diarized_text is not None:
                final_text = diarized_text
                cleanup_meta = diarized_cleanup_meta
            else:
                final_text = cleaned_text

            write_json(
                transcript_json,
                {
                    "text": final_text,
                    "raw_text": merged_text,
                    "entries": entries,
                    "cleanup": cleanup_meta,
                },
            )
            transcript_txt.write_text(final_text, encoding="utf-8")
            st.logger.info(
                "transcript merge cleanup: start_removed=%s end_removed=%s",
                cleanup_meta.get("removed_head_sentences", 0),
                cleanup_meta.get("removed_tail_sentences", 0),
            )

            task = await repo.get_task_by_id(st.task_id)
            if task is None:
                raise RuntimeError("task not found during merge")
            if not task.transcript_path:
                task.transcript_path = str(transcript_txt)
            await session.commit()

        for path in st.dirs["segments"].glob("*.wav"):
            path.unlink(missing_ok=True)
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="phase",
            data={"phase": "merge_transcript", "status": "done"},
        )
        return True
