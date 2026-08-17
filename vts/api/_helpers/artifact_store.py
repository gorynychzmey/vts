"""Reading and resetting what the pipeline wrote to a task's artifact directory.

Archiving a task, resetting summary artifacts for a re-run, and loading the
transcript blocks the player renders.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.base import SUMMARY_STEP_NAMES, _is_path_within
from vts.db.models import StepStatus, Task
from vts.db.repo import Repo
from vts.pipeline.steps.transcription import effective_language

logger = logging.getLogger(__name__)

ARCHIVED_LOG_MESSAGE = "__VTS_LOG_ARCHIVED__"

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
