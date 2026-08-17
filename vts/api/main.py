from __future__ import annotations

import asyncio
import time
import html as _html
import json
import logging
import os
import secrets
import signal
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from starlette.middleware.sessions import SessionMiddleware

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from vts import __version__
from vts.api.deps import (
    get_current_user,
    get_current_user_session_only,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    TaskCompactOut,
    TextSliceOut,
    TaskOut,
)
from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.core.logging import configure_logging
from vts.db.models import StepStatus, Task, TaskStatus
from vts.db.repo import Repo
from vts.pipeline.steps.transcription import effective_language
from vts.services.auth import AuthenticatedUser
from vts.services.media import probe_duration, probe_media
from vts.services.redis_bus import RedisBus
from vts.services import task_status as _ts
from vts.services.task_progress import selected_prompt_refs, summary_progress_for_task


#: Bundled frontend assets. Module-level so routers can serve from it without
#: recomputing a path relative to their own file — vts/api/routers/ is one
#: level deeper, so parents[1] there would point somewhere else entirely.
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def can_pause_task(status: TaskStatus) -> bool:
    return _ts.can_pause(status)


def can_resume_task(status: TaskStatus) -> bool:
    return _ts.can_resume(status)


SUMMARY_STEP_NAMES = frozenset(
    {
        "prepare_llama_model",
        "prepare_summary_chunks",
        "summarize_windows",
        "pack_window_notes",
        "summarize_final",
    }
)


def can_restart_summary_task(task: Task) -> bool:
    refs = selected_prompt_refs(task.options if isinstance(task.options, dict) else {})
    summary_selected = any(r["source"] == "system" and r["id"] == "summary" for r in refs)
    if not summary_selected:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return any(step.name in SUMMARY_STEP_NAMES and step.status == StepStatus.failed for step in task.steps)


def can_restart_final_summary_task(task: Task) -> bool:
    # Mirrors the frontend's `summaryExpected`: ANY selected prompt yields a
    # finalize step (system:summary -> summarize_final, anything else ->
    # finalize:<source>:<id>), so restarting the final summary only requires that
    # at least one prompt is selected. This is deliberately weaker than
    # can_restart_summary_task's system:summary gate above -- restarting *the
    # summary* is meaningless without the summary prompt, but restarting the
    # finalize tail is valid for a user-prompt-only task.
    refs = selected_prompt_refs(task.options if isinstance(task.options, dict) else {})
    if not refs:
        return False
    summarize_windows_status = _find_step_status(task, "summarize_windows")
    if summarize_windows_status != StepStatus.completed:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return _find_step_status(task, "summarize_final") == StepStatus.failed


def can_resolve_speakers_task(task: Task) -> bool:
    """The voice-resolution dialog is available once match_speakers has produced
    speaker_matches.json, for the rest of the task's life except archived/canceled.
    A task-DEPENDENT capability keyed on data availability, which a status set
    can't express.

    Keys off the PRESENCE of speaker_matches.json, NOT the match_speakers step
    status: that step completes for a NON-diarized task too (it early-returns
    without writing the file), so a step-status check would wrongly offer the
    dialog for a task that has no speakers to resolve (the button did nothing).
    The diarized path is the only one that writes the artifact, so its existence
    is the exact precondition — the same file MatchSpeakersStep.already_done and
    the resolve endpoint both read.
    """
    if task.status in {TaskStatus.archived, TaskStatus.canceled}:
        return False
    return (Path(task.artifact_dir) / "outputs" / "speaker_matches.json").exists()


ARCHIVED_LOG_MESSAGE = "__VTS_LOG_ARCHIVED__"


def _is_path_within(root: Path, path: Path) -> bool:
    try:
        root_resolved = root.resolve()
        path_resolved = path.resolve()
    except OSError:
        return False
    return path_resolved == root_resolved or root_resolved in path_resolved.parents


def _archive_task_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return
    try:
        root_resolved = artifact_root.resolve()
    except OSError:
        return

    keep_files: set[Path] = set()
    for raw_path in (task.transcript_path, task.summary_path):
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        if _is_path_within(root_resolved, path):
            keep_files.add(path.resolve())

    log_path = artifact_root / "logs" / "task.log"
    try:
        log_resolved = log_path.resolve(strict=False)
    except OSError:
        log_resolved = log_path

    for file_path in artifact_root.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            file_resolved = file_path.resolve()
        except OSError:
            continue
        if file_resolved in keep_files or file_resolved == log_resolved:
            continue
        file_path.unlink(missing_ok=True)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"{ARCHIVED_LOG_MESSAGE}\n", encoding="utf-8")

    directories = sorted(
        (path for path in artifact_root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        if directory == artifact_root:
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
        except OSError:
            continue


def _reset_summary_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return

    summary_dir = artifact_root / "summary"
    outputs_dir = artifact_root / "outputs"

    for path in summary_dir.glob("window_*.txt"):
        path.unlink(missing_ok=True)

    for path in (
        summary_dir / "chunks.json",
        summary_dir / "windows.json",
        # A full re-summarize regenerates the window summaries, so the packed
        # notes derived from them are stale. Leaving this file makes
        # PackWindowNotesStep.already_done() short-circuit on it and the final
        # step feed the OLD packed text to the LLM — e.g. a speaker renamed
        # after the first run reappears under the old name in the final summary
        # even though the transcript, window summaries and persons list are all
        # fresh (vts-6b4). final_only keeps it on purpose (see
        # _reset_final_summary_artifacts): the window summaries did not change.
        summary_dir / "packed_notes.json",
        summary_dir / "final.json",
        summary_dir / "final.md",
        outputs_dir / "llama_model_ready.json",
        outputs_dir / "summary_chunks.json",
        outputs_dir / "window_summaries.json",
        outputs_dir / "summary.json",
        outputs_dir / "summary.md",
        outputs_dir / "redacted_transcript.txt",
    ):
        path.unlink(missing_ok=True)

    # User-prompt results: without deleting these, FinalizePromptStep
    # short-circuits on the existing files and never regenerates (vts-5eg).
    results_dir = summary_dir / "results"
    if results_dir.exists():
        for path in results_dir.glob("*"):
            path.unlink(missing_ok=True)


def _reset_summary_steps(task: Task) -> None:
    # finalize:* (user-prompt) steps are part of the summary pipeline too:
    # a full restart regenerates their input (the processed transcript), so
    # they must re-run (vts-5eg).
    for step in task.steps:
        if step.name not in SUMMARY_STEP_NAMES and not step.name.startswith("finalize:"):
            continue
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None


def _reset_final_summary_step(task: Task) -> None:
    for step in task.steps:
        if step.name != "summarize_final":
            continue
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None


async def _rebuild_finalize_tail(repo: Repo, task: Task, new_options: dict) -> None:
    """Rebuild the finalize tail (post-DAG_HEAD steps) for ``new_options``.

    Deletes finalize-step rows (``summarize_final`` or ``finalize:*``) that are
    no longer in the target tail, and upserts each target-tail step forced to
    pending. Head steps are left untouched.
    """
    from vts.pipeline.types import DAG_HEAD, build_dag_steps

    target_tail = [s for s in build_dag_steps(new_options) if s not in DAG_HEAD]
    current_final = [
        st.name
        for st in task.steps
        if st.name == "summarize_final" or st.name.startswith("finalize:")
    ]
    to_delete = [n for n in current_final if n not in target_tail]
    await repo.delete_steps_by_name(task.id, to_delete)
    for name in target_tail:
        step = await repo.upsert_step(task.id, name)
        step.status = StepStatus.pending
        step.attempt = 0
        step.started_at = None
        step.finished_at = None
        step.message = None
    await repo.session.flush()


def _reset_final_summary_artifacts(task: Task) -> None:
    artifact_root = Path(task.artifact_dir)
    if not artifact_root.exists():
        return
    summary_dir = artifact_root / "summary"
    outputs_dir = artifact_root / "outputs"
    for path in (
        summary_dir / "final.json",
        summary_dir / "final.md",
        outputs_dir / "summary.json",
        outputs_dir / "summary.md",
    ):
        path.unlink(missing_ok=True)


def _find_step_status(task: Task, step_name: str) -> StepStatus | None:
    for step in task.steps:
        if step.name == step_name:
            return step.status
    return None


def _speaker_ordering_entries(outputs: Path, matches: dict) -> list[dict]:
    """Entries in first-appearance order for label_map, so the dialog's
    display_label matches the transcript's "Голос N" numbering (bug #2).

    Prefer diarization.json segments — they cover EVERY speaker in first-spoke
    order, including ones the transcript dropped as noise, so a noise speaker
    still gets a stable number. Fall back to transcript.json entries, then to
    the matches keys (so a label always gets some ordering). label_map only
    reads each item's "speaker", so a bare {"speaker": label} list suffices.
    """
    diar_path = outputs / "diarization.json"
    if diar_path.exists():
        try:
            diar = json.loads(diar_path.read_text(encoding="utf-8"))
            segments = diar.get("segments") if isinstance(diar, dict) else None
            if isinstance(segments, list) and segments:
                return [{"speaker": s.get("speaker")} for s in segments
                        if isinstance(s, dict) and s.get("speaker") is not None]
        except (OSError, json.JSONDecodeError):
            pass
    transcript_path = outputs / "transcript.json"
    if transcript_path.exists():
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, list) and entries:
                return [{"speaker": e.get("speaker")} for e in entries
                        if isinstance(e, dict) and e.get("speaker") is not None]
        except (OSError, json.JSONDecodeError):
            pass
    return [{"speaker": label} for label in matches]


def _processing_seconds_for_task(task: Task) -> int | None:
    # Sum each step's own duration rather than the span from the first step's
    # start to the last step's finish. A task can sit paused / awaiting input
    # for hours between steps; that idle gap is not work time, so counting the
    # raw span (max(finished) - min(started)) massively overstates it. Each
    # step carries its own started_at/finished_at (last attempt), so the sum of
    # per-step durations is the actual processing time. Pauses that happen
    # *inside* a running step are not subtracted (the step re-runs with a fresh
    # started_at on resume anyway) — only between-step idle time is excluded.
    total = 0.0
    counted = False
    for step in task.steps:
        if step.started_at is None or step.finished_at is None:
            continue
        step_seconds = (step.finished_at - step.started_at).total_seconds()
        if step_seconds < 0:
            continue
        total += step_seconds
        counted = True
    if not counted:
        return None
    return int(total)


def _text_length_from_path(path_value: str | Path | None, *, prefer_json_text_field: bool = False) -> int | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    text_value = raw_text
    if prefer_json_text_field and path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        extracted = payload.get("text")
        if not isinstance(extracted, str):
            return None
        text_value = extracted

    return len(text_value.strip())


def _task_stats_for_serialization(task: Task) -> dict[str, int | None]:
    redacted_path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
    media_file = _find_media_file(task.artifact_dir)
    media_bytes: int | None = None
    media_seconds: int | None = None
    if media_file is not None:
        try:
            media_bytes = media_file.stat().st_size
        except OSError:
            media_bytes = None
        media_seconds = _media_seconds_for_file(media_file)
    return {
        "processing_seconds": _processing_seconds_for_task(task),
        "transcript_chars": _text_length_from_path(task.transcript_path, prefer_json_text_field=True),
        "summary_chars": _text_length_from_path(task.summary_path, prefer_json_text_field=False),
        "redacted_chars": _text_length_from_path(redacted_path, prefer_json_text_field=False),
        "media_seconds": media_seconds,
        "media_bytes": media_bytes,
    }


_MAX_DISPLAY_NAME_CHARS = 500  # matches Text column; keep titles sane


def normalize_display_name(raw: str | None) -> str | None:
    """Normalize a user-supplied task title. Empty/whitespace-only input
    becomes None (so the UI falls back to source_url); otherwise trim
    surrounding whitespace and cap length to keep titles bounded."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_DISPLAY_NAME_CHARS]


_QUEUE_POS_CACHE_SUFFIX = "cache:queue_positions"
_QUEUE_POS_TTL_SECONDS = 2


async def _get_cached_queue_positions(
    redis: Redis, repo: Repo, prefix: str
) -> dict[uuid.UUID, int]:
    cache_key = f"{prefix}{_QUEUE_POS_CACHE_SUFFIX}"
    cached = await redis.get(cache_key)
    if cached is not None:
        raw: dict[str, int] = json.loads(cached)
        return {uuid.UUID(k): v for k, v in raw.items()}
    positions = await repo.get_global_queue_positions()
    serializable = {str(k): v for k, v in positions.items()}
    await redis.setex(cache_key, _QUEUE_POS_TTL_SECONDS, json.dumps(serializable))
    return positions


async def _get_lane_positions(redis: Redis, prefix: str) -> dict[uuid.UUID, tuple[str, int]]:
    raw = await redis.get(f"{prefix}queue:lanes")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[uuid.UUID, tuple[str, int]] = {}
    # network and ffmpeg map to their own distinct public queue names, each
    # with an independent counter. gpu_asr and gpu_llm both map to the
    # public "gpu" queue and share ONE counter — asr is numbered first since
    # it has scheduling priority in LaneManager, so an asr-waiting task
    # always gets a lower position than an llm-waiting task.
    groups: list[tuple[str, list[str]]] = [
        ("network", ["network"]),
        ("ffmpeg", ["ffmpeg"]),
        ("gpu", ["gpu_asr", "gpu_llm"]),
        ("diarize", ["diarize"]),
    ]
    for public, keys in groups:
        position = 0
        for key in keys:
            entries = data.get(key, [])
            if not isinstance(entries, list):
                continue
            for raw_id in entries:
                try:
                    tid = uuid.UUID(raw_id)
                except (ValueError, TypeError, AttributeError):
                    continue
                if tid in out:
                    continue
                position += 1
                out[tid] = (public, position)
    return out


def _operator_block_html(settings: Settings) -> str:
    """Build the operator-specific block that prepends the rendered
    privacy page. Falls back to a neutral note if no operator details
    are configured."""
    name = (settings.operator_name or "").strip()
    contact = (settings.operator_contact or "").strip()
    instance = (settings.operator_instance_name or "").strip()
    if not any((name, contact, instance)):
        return (
            "<aside class='operator-block'>"
            "<p><em>This deployment did not publish operator details. "
            "Ask whoever gave you the link for their contact channel "
            "and access policy.</em></p>"
            "</aside>"
        )
    parts = ["<aside class='operator-block'>", "<h2>On this deployment</h2>", "<ul>"]
    if instance:
        parts.append(f"<li><strong>Instance:</strong> {_html.escape(instance)}</li>")
    if name:
        parts.append(f"<li><strong>Operator:</strong> {_html.escape(name)}</li>")
    if contact:
        parts.append(f"<li><strong>Contact:</strong> {_html.escape(contact)}</li>")
    parts.extend(["</ul>", "</aside>"])
    return "".join(parts)


_PRIVACY_MD_PATH = Path(__file__).resolve().parents[1] / ".." / "PRIVACY.md"
_PRIVACY_TEMPLATE_HTML: str | None = None


def _privacy_template_html() -> str:
    """Read PRIVACY.md off disk once and convert to HTML."""
    global _PRIVACY_TEMPLATE_HTML
    if _PRIVACY_TEMPLATE_HTML is not None:
        return _PRIVACY_TEMPLATE_HTML
    from markdown_it import MarkdownIt
    try:
        md_text = _PRIVACY_MD_PATH.resolve().read_text(encoding="utf-8")
    except FileNotFoundError:
        md_text = "# Privacy policy\n\n_PRIVACY.md not found in deployment._\n"
    _PRIVACY_TEMPLATE_HTML = MarkdownIt("commonmark").render(md_text)
    return _PRIVACY_TEMPLATE_HTML


def _render_privacy_page(settings: Settings) -> str:
    """Render the public /privacy HTML — operator block + rendered template."""
    operator_html = _operator_block_html(settings)
    body = _privacy_template_html()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Privacy policy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 740px;
    margin: 2rem auto; padding: 0 1rem; line-height: 1.55;
    color: #222; background: #fafaf7; }}
  h1, h2, h3 {{ margin-top: 1.6em; }}
  h1 {{ font-size: 1.8rem; }}
  h2 {{ font-size: 1.3rem; }}
  table {{ border-collapse: collapse; margin: 1em 0; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.7rem; text-align: left; }}
  code {{ background: #eee; padding: 0 0.25em; border-radius: 3px; }}
  aside.operator-block {{
    border: 1px solid #c89; background: #fdf6f0;
    border-radius: 6px; padding: 0.75rem 1rem; margin: 1.5rem 0;
  }}
  aside.operator-block h2 {{ margin: 0 0 0.5em; font-size: 1.1rem; }}
  aside.operator-block ul {{ margin: 0; padding-left: 1.2em; }}
</style>
</head>
<body>
{operator_html}
{body}
</body>
</html>"""


def _user_hash_dir(username: str) -> str:
    from vts.services.storage import user_hash
    return user_hash(username)


def _find_media_file(artifact_dir: str | None) -> Path | None:
    if not artifact_dir:
        return None
    media_dir = Path(artifact_dir) / "media"
    # audio.combined.* rather than a fixed .wav: a stream-copied set keeps the
    # uploaded encoding, so the extension varies (vts-08q). It must still come
    # after video.mkv and before the raw parts.
    for pattern in ("video.mkv", "audio.combined.*", "audio.original.*"):
        matches = sorted(
            p for p in (media_dir.glob(pattern) if media_dir.exists() else [])
            # Skip our own probe sidecar (audio.original.*.probe.json), which
            # the wildcard would otherwise pick up as the "media" file.
            if not p.name.endswith(".probe.json")
        )
        if matches:
            return matches[-1]
    return None


def _load_transcript_entries(artifact_dir: str | None) -> list[dict[str, Any]]:
    """Read outputs/transcript.json and return its `entries` list (each
    {start, end, text, speaker}), or [] when absent/malformed. Powers the
    clickable transcript on the /player page (vts-at8)."""
    if not artifact_dir:
        return []
    path = Path(artifact_dir) / "outputs" / "transcript.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _load_raw_segments(artifact_dir: str | None) -> list[dict[str, Any]]:
    """Read asr/segments_raw.json's `segments` list (each carries the chunk
    offset + the inner ASR segments with per-sentence timings), or [] when
    absent. Powers the sentence-level split on the /player page (vts-u6w)."""
    if not artifact_dir:
        return []
    path = Path(artifact_dir) / "asr" / "segments_raw.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return []
    return [s for s in segments if isinstance(s, dict)]


async def _load_player_blocks(task: Any, session: AsyncSession) -> list[dict[str, Any]]:
    """Two-level transcript for /player: existing blocks (ASR segment / speaker
    turn) with clickable sentences inside, and a resolved speaker label instead
    of the raw SPEAKER_NN tag (vts-u6w). All derived on the fly from the stored
    artifacts + registry names — transcript.json itself is never rewritten."""
    from vts.services.diarization.merge import speaker_label_word
    from vts.services.player_transcript import build_player_blocks

    entries = _load_transcript_entries(task.artifact_dir)
    if not entries:
        return []
    raw_segments = _load_raw_segments(task.artifact_dir)

    repo = Repo(session)
    names = await repo.speaker_names_for_task(task.user_id, task.id)
    language = effective_language(
        task.options if isinstance(task.options, dict) else {},
        {"outputs": Path(task.artifact_dir) / "outputs"},
    )
    return build_player_blocks(
        entries,
        raw_segments,
        names=names,
        label_word=speaker_label_word(language),
    )


def _media_seconds_for_file(media_file: Path) -> int | None:
    """Media (audio/video) length in whole seconds, probed via ffprobe.

    ffprobe spawns a subprocess, so the result is cached in a sidecar JSON
    keyed on the media file's size+mtime. List serialization probes each
    task at most once per file; later renders read the sidecar."""
    try:
        stat = media_file.stat()
    except OSError:
        return None
    sidecar = media_file.with_suffix(media_file.suffix + ".probe.json")
    cache_key = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    try:
        cached = json.loads(sidecar.read_text(encoding="utf-8"))
        if (
            isinstance(cached, dict)
            and cached.get("size") == cache_key["size"]
            and cached.get("mtime_ns") == cache_key["mtime_ns"]
            and isinstance(cached.get("seconds"), int)
        ):
            return cached["seconds"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        seconds = int(probe_duration(media_file))
    except (RuntimeError, ValueError):
        return None
    if seconds < 0:
        seconds = 0
    try:
        sidecar.write_text(json.dumps({**cache_key, "seconds": seconds}), encoding="utf-8")
    except OSError:
        pass  # best-effort cache; still return the freshly probed value
    return seconds


def serialize_task(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    lane_positions: dict[uuid.UUID, tuple[str, int]] | None = None,
) -> TaskOut:
    queue: str | None = None
    queue_position: int | None = None
    if task.status == TaskStatus.waiting and lane_positions is not None and task.id in lane_positions:
        queue, queue_position = lane_positions[task.id]
    elif queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    failure_code = classify_failure_code(task.error_message)
    return TaskOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        awaiting_step=task.awaiting_step,
        queue_position=queue_position,
        queue=queue,
        capabilities={
            "can_restart_summary": can_restart_summary_task(task),
            "can_restart_final_summary": can_restart_final_summary_task(task),
            "can_resolve_speakers": can_resolve_speakers_task(task),
        },
        options=task.options,
        transcript_path=task.transcript_path,
        summary_path=task.summary_path,
        redacted_path=str(Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt")
        if task.artifact_dir
        and (Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt").exists()
        else None,
        media_path=str(_mf) if (_mf := _find_media_file(task.artifact_dir)) else None,
        error_message=task.error_message,
        failure_code=failure_code,
        created_at=task.created_at,
        updated_at=task.updated_at,
        steps=[
            {
                "name": step.name,
                "status": step.status.value,
                "attempt": step.attempt,
                "started_at": step.started_at,
                "finished_at": step.finished_at,
                "message": step.message,
            }
            for step in sorted(task.steps, key=lambda item: item.name)
        ],
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
        stats=_task_stats_for_serialization(task),
    )


def serialize_task_compact(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    lane_positions: dict[uuid.UUID, tuple[str, int]] | None = None,
) -> "TaskCompactOut":
    """Compact serializer for list views. Drops steps/options/paths/error
    message — see TaskCompactOut docstring for the rationale."""
    from vts.api.schemas import TaskCompactOut
    queue: str | None = None
    queue_position: int | None = None
    if task.status == TaskStatus.waiting and lane_positions is not None and task.id in lane_positions:
        queue, queue_position = lane_positions[task.id]
    elif queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    return TaskCompactOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        awaiting_step=task.awaiting_step,
        queue_position=queue_position,
        queue=queue,
        capabilities={
            "can_restart_summary": can_restart_summary_task(task),
            "can_restart_final_summary": can_restart_final_summary_task(task),
            "can_resolve_speakers": can_resolve_speakers_task(task),
        },
        failure_code=classify_failure_code(task.error_message),
        created_at=task.created_at,
        updated_at=task.updated_at,
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
        stats=_task_stats_for_serialization(task),
    )


def _resolve_session_secret(*, env_secret: str | None, secret_file: Path) -> str:
    """Resolve the SessionMiddleware HMAC key.

    Priority:
      1. VTS_SESSION_SECRET env (explicit / HA / multi-host deployments).
      2. Contents of secret_file. Auto-created on first start so a fresh
         self-hosted install does not require manual key generation.

    On first start the file is written with mode 0600 via O_EXCL so
    parallel uvicorn workers cannot both write — the loser of the race
    catches FileExistsError and reads what the winner wrote.
    """
    if env_secret:
        return env_secret

    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(secret_file), flags, 0o600)
    except FileExistsError:
        # Another worker won the race; read its value.
        return secret_file.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_secret)
    except Exception:
        # On any write failure, remove the half-written file so the next
        # start retries cleanly rather than reading an empty secret.
        try:
            secret_file.unlink()
        except OSError:
            pass
        raise
    logging.getLogger(__name__).info(
        "generated new session secret at %s", secret_file
    )
    return new_secret


def _downgrade_to_openapi_30(node: Any) -> Any:
    """Convert OpenAPI 3.1 nullable forms into 3.0-compatible
    `{type: ..., nullable: true}` recursively.

    ChatGPT Custom Actions advertise support for OpenAPI 3.1.x but their
    response-validation pipeline chokes on the 3.1 nullable form
    `anyOf: [{type: "string"}, {type: "null"}]` — clients see
    `ClientResponseError` even though our server returned 200 OK. The
    fix is to rewrite those constructs to the older
    `{type: "string", nullable: true}` shape and downgrade the spec
    version string to 3.0.3.

    Pydantic v2 emits the 3.1 form unconditionally, so we transform the
    spec after FastAPI builds it.
    """
    if isinstance(node, dict):
        # Case: anyOf/oneOf containing a `{type: "null"}` sibling.
        for key in ("anyOf", "oneOf"):
            variants = node.get(key)
            if isinstance(variants, list):
                null_variants = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("type") == "null"
                ]
                non_null = [
                    v for v in variants
                    if not (isinstance(v, dict) and v.get("type") == "null")
                ]
                if null_variants and non_null:
                    # If exactly one non-null branch remains, inline it and
                    # mark nullable. Otherwise wrap the surviving branches
                    # back into the anyOf/oneOf with a nullable sibling
                    # (rare in our spec).
                    if len(non_null) == 1:
                        # Drop the anyOf wrapper, merge its single branch
                        # into the parent, and set nullable on the result.
                        node.pop(key)
                        for k, v in non_null[0].items():
                            node.setdefault(k, v)
                        node["nullable"] = True
                    else:
                        node[key] = non_null
                        node["nullable"] = True
        # Case: 3.1 union "type": ["string", "null"]
        t = node.get("type")
        if isinstance(t, list):
            non_null_types = [x for x in t if x != "null"]
            if len(non_null_types) == 1:
                node["type"] = non_null_types[0]
                if "null" in t:
                    node["nullable"] = True
            elif "null" in t:
                node["type"] = non_null_types
                node["nullable"] = True
        # Recurse.
        for v in node.values():
            _downgrade_to_openapi_30(v)
    elif isinstance(node, list):
        for item in node:
            _downgrade_to_openapi_30(item)
    return node


def _install_custom_openapi(app: FastAPI, settings: Settings) -> None:
    """Override app.openapi() so the generated spec is suitable for
    external clients (e.g. GPT Custom Actions, curl/Postman).

    On top of FastAPI's auto-generated spec we add:
      - `servers` with the deployment's public base URL (if configured)
      - `securitySchemes.ApiToken` (HTTP Bearer) + global default security
      - Per-path tags grouped by URL prefix (tasks, meta, admin)
      - Downgrade 3.1 nullable form to 3.0-compat for client compatibility
    """
    from fastapi.openapi.utils import get_openapi

    def _tag_for_path(path: str) -> str:
        if path.startswith("/api/tasks"):
            return "tasks"
        if path.startswith("/api/admin"):
            return "admin"
        return "meta"

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        if settings.public_base_url:
            schema["servers"] = [{"url": settings.public_base_url.rstrip("/")}]
        # Schemas referenced only via responses[...]['content']['$ref']
        # don't get auto-collected by FastAPI; inject them explicitly so
        # OpenAPI consumers can resolve the $ref.
        components = schema.setdefault("components", {})
        registered_schemas = components.setdefault("schemas", {})
        for extra_model in (TextSliceOut,):
            name = extra_model.__name__
            if name not in registered_schemas:
                registered_schemas[name] = extra_model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
        components["securitySchemes"] = {
            "ApiToken": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Personal API token issued from the VTS UI "
                    "(header → key icon → Create token). Format: `vts_<43 chars>`. "
                    "Browser session cookies also work for the same endpoints but "
                    "are out of scope for external clients."
                ),
            }
        }
        # Apply globally; unauthenticated endpoints opt out individually below.
        schema["security"] = [{"ApiToken": []}]
        for path, methods in schema.get("paths", {}).items():
            tag = _tag_for_path(path)
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                op.setdefault("tags", [tag])
        # Endpoints that must NOT require auth in the spec.
        for path in ("/api/version", "/api/status-config", "/healthz"):
            for op in schema.get("paths", {}).get(path, {}).values():
                if isinstance(op, dict):
                    op["security"] = []
        # Rewrite the 3.1 nullable form `anyOf: [..., {type: null}]` to the
        # widely-supported `nullable: true` extension. ChatGPT Custom Actions
        # validator chokes on the former even though it parses fine
        # elsewhere; the latter is accepted by both 3.0.x and 3.1.x clients
        # in practice. We keep the 3.1.0 header so ChatGPT's "must be
        # 3.1.0/3.1.1" check passes, even though `nullable` is technically a
        # 3.0 leftover — most validators (incl. ChatGPT, Swagger UI, Redoc)
        # honour it regardless of declared version.
        _downgrade_to_openapi_30(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


_ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset(
    {
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
    }
)


def _normalize_delivery_json(delivery: str | None) -> list[dict]:
    """Parse the `delivery` form field of an upload into entry dicts.

    Uploads carry their options as form fields / JSON sidecars rather than a
    request model, so `delivery` arrives as a JSON string and needs the same
    shape check the URL path gets from DeliveryRef. Ownership and adapter
    availability are validated later, by validate_delivery_refs, exactly as on
    the URL path.
    """
    if not delivery:
        return []
    try:
        raw = json.loads(delivery)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="delivery must be valid JSON") from exc
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="delivery must be a JSON list")
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail="each delivery entry must be an object")
        ref = entry.get("deliver_to")
        if not ref:
            raise HTTPException(status_code=422, detail="delivery entry requires 'deliver_to'")
        item: dict = {"deliver_to": str(ref)}
        variant = entry.get("variant")
        if variant:
            if variant not in ("raw", "redacted", "summary"):
                # May also be a prompt ref like "user:<uuid>" (vts-as1i).
                from vts.services.prompt_registry import parse_ref

                try:
                    parse_ref(str(variant))
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"invalid delivery variant: {variant!r}"
                    ) from exc
            item["variant"] = str(variant)
        out.append(item)
    return out


def _normalize_prompts_json(prompts: str | None) -> list[dict]:
    from vts.services.prompt_registry import parse_ref, ref_to_dict
    if prompts is None:
        return [{"source": "system", "id": "summary"}]
    try:
        raw_refs = json.loads(prompts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="prompts must be valid JSON") from exc
    if not isinstance(raw_refs, list):
        raise HTTPException(status_code=422, detail="prompts must be a JSON list")
    out: list[dict] = []
    for entry in raw_refs:
        try:
            source, ref_id = parse_ref(entry)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid prompt ref: {entry!r}") from exc
        out.append(ref_to_dict(source, ref_id))
    return out


async def _enqueue_uploaded_task(task, repo, redis, settings) -> "TaskOut":
    bus = RedisBus(redis, settings)
    await bus.notify_queued()
    await bus.publish_event(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event="task_status",
        data={"status": task.status.value},
    )
    set_committed_value(task, "steps", [])
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    lane_positions = await _get_lane_positions(redis, settings.redis_prefix)
    asr_progress = await repo.get_asr_progress_for_tasks([task.id])
    summary_progress = {task.id: summary_progress_for_task(task)}
    return serialize_task(task, queue_positions, asr_progress, summary_progress, lane_positions)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings_dep()

    if settings.oauth_enabled:
        if not settings.oauth_client_secret:
            raise RuntimeError(
                "oauth_enabled=True but oauth_client_secret is missing — "
                "set VTS_OAUTH_CLIENT_SECRET"
            )
        session_secret = _resolve_session_secret(
            env_secret=settings.session_secret,
            secret_file=settings.session_secret_file,
        )

    # Build the MCP sub-app eagerly so we can chain its lifespan into ours;
    # FastAPI does not run lifespans of mounted sub-apps, and the FastMCP
    # streamable-http transport initialises its session manager only via
    # that lifespan.
    mcp_app = None
    mcp_oauth_routes: list = []
    if settings.mcp_enabled:
        from vts.mcp import build_mcp_app_with_wellknown
        mcp_app, mcp_oauth_routes = build_mcp_app_with_wellknown(settings.mcp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Watched by long-lived streams (/api/events) so they can end
        # themselves. Without it uvicorn's graceful shutdown waits on SSE
        # clients that never disconnect, so the container only died once
        # --timeout-graceful-shutdown expired and SIGKILL arrived (vts-9er).
        app.state.shutting_down = asyncio.Event()
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        # Setting the flag here in the `finally` is too late to be useful on
        # its own: uvicorn waits for open connections BEFORE running the
        # lifespan, so an idle SSE stream held the stop for the whole
        # --timeout-graceful-shutdown (measured twice on prod: 15s).
        #
        #   connection.shutdown() for each connection
        #   await asyncio.wait_for(_wait_tasks_to_complete(), timeout=...)  <- waits
        #   await self.lifespan.shutdown()                                  <- too late
        #
        # uvicorn installs its own SIGTERM/SIGINT handler with plain
        # signal.signal (server.py:319) and that handler only flips
        # `should_exit`. So we chain ours in front of it: ours fires at signal
        # delivery, before shutdown() is entered, and the streams end
        # themselves while uvicorn is still closing listeners. Measured with
        # one live SSE client: 15.19s without this, 0.19s with it.
        loop = asyncio.get_running_loop()
        previous: dict[int, Any] = {}

        def _note_shutdown(sig: int, frame: Any) -> None:
            # Runs in the signal context, so only schedule work on the loop.
            loop.call_soon_threadsafe(app.state.shutting_down.set)
            chained = previous.get(sig)
            if callable(chained):
                chained(sig, frame)

        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _note_shutdown)
            except ValueError:
                # Not the main thread (tests, embedded runs): the flag still
                # gets set by the lifespan below, just as late as before.
                previous.pop(sig, None)
            else:
                installed.append(sig)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            app.state.shutting_down.set()
            for sig in installed:
                # Hand the signal back, so uvicorn's own restore in
                # capture_signals() puts back what was there before us.
                with suppress(ValueError):
                    signal.signal(sig, previous[sig])
            await app.state.redis.aclose()

    app = FastAPI(
        title="vts",
        version=__version__,
        description=(
            "Self-hosted video transcription and summarisation API. "
            "Authenticate with a personal API token from the VTS web UI "
            "(header → key icon → Create token). "
            "Send it as `Authorization: Bearer vts_…`. "
            "See https://github.com/gorynychzmey/vts/blob/main/docs/AUTH.md "
            "for the full auth model and "
            "https://github.com/gorynychzmey/vts/blob/main/docs/API.md "
            "for programmatic-access details (incl. GPT Custom Actions)."
        ),
        lifespan=lifespan,
    )
    _install_custom_openapi(app, settings)

    if settings.oauth_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            session_cookie="vts_session",
            https_only=True,
            same_site="lax",
            max_age=settings.session_max_age_days * 86_400,
        )

    if settings.oauth_enabled:
        from vts.api.auth_routes import router as auth_router
        app.include_router(auth_router)

    # FastMCP's OAuth routes (/.well-known/oauth-*, /authorize, /token,
    # /register, /consent, /<mcp_path>/auth/callback) all live at host
    # root per RFC 8414/9728. Mount them on the parent FastAPI BEFORE the
    # MCP sub-app so they win path matching.
    for route in mcp_oauth_routes:
        app.router.routes.append(route)

    static_dir = STATIC_DIR
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if mcp_app is not None:
        app.mount(settings.mcp_path, mcp_app)

    # Domain routers. Imported here rather than at module scope: they reach
    # back into this module for helpers that have not moved yet, so a
    # top-level import would be a cycle (docs/plans/main-py-split.md).
    #
    # Order is the order FastAPI matches in. It matters where a literal path
    # competes with a parameterised one, so keep related prefixes together and
    # do not reshuffle these lines casually.
    from vts.api.routers.artifacts import router as artifacts_router
    from vts.api.routers.delivery import router as delivery_router
    from vts.api.routers.meta import router as meta_router
    from vts.api.routers.pages import router as pages_router
    from vts.api.routers.speakers import router as speakers_router
    from vts.api.routers.tasks import router as tasks_router
    from vts.api.routers.uploads import router as uploads_router

    for domain_router in (
        pages_router,
        meta_router,
        delivery_router,
        tasks_router,
        uploads_router,
        artifacts_router,
        speakers_router,
    ):
        app.include_router(domain_router)


    return app


app = create_app()
