from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.middleware.sessions import SessionMiddleware

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from vts import __version__
from vts.api.csrf import require_same_site
from vts.api.deps import (
    get_current_user,
    get_current_user_session_only,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    AdminUsersOut,
    ApiTokenCreateOut,
    ApiTokenCreateRequest,
    ApiTokenOut,
    BatchResultOut,
    MeOut,
    PromptCreateRequest,
    PromptDetailOut,
    PromptOut,
    PromptUpdateRequest,
    SystemPromptTextOut,
    TaskCompactOut,
    TextSliceOut,
    PushConfigOut,
    PushStatusOut,
    PushSubscriptionIn,
    PushUnsubscribeIn,
    RestartSummaryRequest,
    TaskCreateRequest,
    TaskIdsRequest,
    TaskOut,
    TaskUpdate,
)
from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.core.logging import configure_logging
from vts.db.models import StepStatus, Task, TaskStatus
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.media import probe_duration
from vts.services.media_kind import media_content_type, media_kind
from vts.services.push import (
    SubscriptionPayload,
    delete_subscription,
    is_push_enabled,
    list_subscriptions,
    upsert_subscription,
)
from vts.services.redis_bus import RedisBus
from vts.services.storage import task_dir
from vts.services.task_progress import selected_prompt_refs, summary_progress_for_task


def can_pause_task(status: TaskStatus) -> bool:
    return status in {TaskStatus.queued, TaskStatus.running}


def can_resume_task(status: TaskStatus) -> bool:
    return status in {TaskStatus.paused, TaskStatus.failed}


SUMMARY_STEP_NAMES = frozenset(
    {
        "prepare_llama_model",
        "prepare_summary_chunks",
        "summarize_windows",
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
    summarize_windows_status = _find_step_status(task, "summarize_windows")
    if summarize_windows_status != StepStatus.completed:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return _find_step_status(task, "summarize_final") == StepStatus.failed


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


def _reset_summary_steps(task: Task) -> None:
    for step in task.steps:
        if step.name not in SUMMARY_STEP_NAMES:
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



def _processing_seconds_for_task(task: Task) -> int | None:
    started = [step.started_at for step in task.steps if step.started_at is not None]
    finished = [step.finished_at for step in task.steps if step.finished_at is not None]
    if not started or not finished:
        return None
    duration = (max(finished) - min(started)).total_seconds()
    if duration < 0:
        return 0
    return int(duration)


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


_MAX_TEXT_SLICE_CHARS = 200_000  # safety cap for JSON-mode slice length


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


def _parse_range_header(value: str, total: int) -> tuple[int, int] | None:
    """Parse a `Range: bytes=START-END` header into (offset, length) char-pair.

    We use character offsets (not bytes) since the underlying artifacts are
    UTF-8 text and we want predictable slicing. Returns None for malformed
    or unsatisfiable ranges; the caller falls back to a full response.
    """
    if not value:
        return None
    value = value.strip().lower()
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):]
    if "," in spec:
        return None  # multipart ranges — not supported
    if "-" not in spec:
        return None
    start_str, end_str = spec.split("-", 1)
    # We deliberately do not support suffix-range (`bytes=-N`, "last N
    # bytes") — start_str must be present. Callers wanting the tail can
    # compute the offset from total_length.
    if not start_str:
        return None
    try:
        start = int(start_str)
        end = int(end_str) if end_str else total - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total:
        return None
    end = min(end, total - 1)
    return start, (end - start + 1)


def _serve_text(
    text: str,
    plain_media_type: str,
    *,
    request: Request,
    offset: int | None,
    limit: int | None,
) -> Response:
    """Serve a text artifact with three modes:

    1. Default (Accept: text/plain or */*; no slicing) → full body, original
       media-type (text/plain or text/markdown). Unchanged behaviour.
    2. Range header (`bytes=START-END`) → 206 Partial Content, plain text
       slice. Standard HTTP, works for curl/wget/anything HTTP-literate.
    3. Accept: application/json (+ optional ?offset/?limit) → JSON
       TextSliceOut with metadata. Works around 30KB client caps
       (notably ChatGPT Custom Actions). Slicing applied if requested,
       else full text wrapped in JSON.
    """
    total = len(text)

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range_header(range_header, total)
        if parsed is not None:
            start, length = parsed
            chunk = text[start:start + length]
            return Response(
                content=chunk,
                status_code=206,
                media_type=plain_media_type,
                headers={
                    "Content-Range": f"bytes {start}-{start + length - 1}/{total}",
                    "Accept-Ranges": "bytes",
                },
            )

    accept = (request.headers.get("accept") or "").lower()
    wants_json = "application/json" in accept and "text/plain" not in accept
    has_slice_query = offset is not None or limit is not None

    if wants_json or has_slice_query:
        off = max(0, offset or 0)
        if off > total:
            off = total
        lim = limit if limit is not None else _MAX_TEXT_SLICE_CHARS
        lim = max(0, min(lim, _MAX_TEXT_SLICE_CHARS))
        slice_text = text[off:off + lim]
        payload = TextSliceOut(
            text=slice_text,
            offset=off,
            length=len(slice_text),
            total_length=total,
            is_end=(off + len(slice_text)) >= total,
        )
        return JSONResponse(payload.model_dump(), headers={"Accept-Ranges": "bytes"})

    # Default: full plain text, as before.
    return Response(content=text, media_type=plain_media_type, headers={"Accept-Ranges": "bytes"})


def _find_media_file(artifact_dir: str | None) -> Path | None:
    if not artifact_dir:
        return None
    media_dir = Path(artifact_dir) / "media"
    for pattern in ("video.mkv", "audio.original.*"):
        matches = sorted(
            p for p in (media_dir.glob(pattern) if media_dir.exists() else [])
            # Skip our own probe sidecar (audio.original.*.probe.json), which
            # the wildcard would otherwise pick up as the "media" file.
            if not p.name.endswith(".probe.json")
        )
        if matches:
            return matches[-1]
    return None


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
) -> TaskOut:
    queue_position: int | None = None
    if queue_positions is not None:
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
        queue_position=queue_position,
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
) -> "TaskCompactOut":
    """Compact serializer for list views. Drops steps/options/paths/error
    message — see TaskCompactOut docstring for the rationale."""
    from vts.api.schemas import TaskCompactOut
    queue_position: int | None = None
    if queue_positions is not None:
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
        queue_position=queue_position,
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
        for path in ("/api/version", "/healthz"):
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
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
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

    static_dir = Path(__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if mcp_app is not None:
        app.mount(settings.mcp_path, mcp_app)

    no_cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        if settings.oauth_enabled:
            session_data = getattr(request, "session", None) or {}
            if not isinstance(session_data, dict):
                session_data = {}
            # vts-pa9: prefer sid (current cookie shape); fall back to
            # legacy email (cookies issued before vts-pa9). Either presence
            # means the user has a session — the resolver will validate it
            # on the next authenticated call.
            has_session = bool(
                (session_data.get("sid") or "").strip()
                or (session_data.get("email") or "").strip()
            )
            if not has_session:
                import urllib.parse
                return RedirectResponse(
                    url=f"/auth/login?next={urllib.parse.quote(request.url.path, safe='')}",
                    status_code=302,
                )
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        content = template.replace("__VTS_VERSION__", __version__)
        return HTMLResponse(content=content, headers=no_cache_headers)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            path=str(static_dir / "manifest.webmanifest"),
            media_type="application/manifest+json",
        )

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        # Serve service worker from root so its scope covers the whole app.
        return FileResponse(
            path=str(static_dir / "sw.js"),
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.post("/share", include_in_schema=False)
    async def share_target_post() -> RedirectResponse:
        # POST /share is normally intercepted by the service worker, which
        # stashes any shared file and redirects the client. If the SW isn't
        # active yet (first launch after install), fall back to the root so
        # the user at least lands in the app.
        return RedirectResponse(url="/?share_error=sw_not_ready", status_code=303)

    @app.get("/share", include_in_schema=False)
    async def share_target(
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
    ) -> RedirectResponse:
        # Android share sheet passes arbitrary payloads. YouTube typically
        # puts the URL into `text`. Forward everything and let the frontend
        # pick the best candidate.
        params: dict[str, str] = {}
        if url:
            params["share_url"] = url
        if text:
            params["share_text"] = text
        if title:
            params["share_title"] = title
        query = f"?{urlencode(params)}" if params else ""
        return RedirectResponse(url=f"/{query}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/privacy", include_in_schema=False, response_class=HTMLResponse)
    async def privacy_policy(
        settings: Settings = Depends(get_settings_dep),
    ) -> HTMLResponse:
        return HTMLResponse(_render_privacy_page(settings))

    @app.get("/api/version")
    async def version() -> JSONResponse:
        return JSONResponse({"version": __version__}, headers=no_cache_headers)

    @app.get("/api/me", response_model=MeOut)
    async def me(user: AuthenticatedUser = Depends(get_current_user)) -> MeOut:
        return MeOut(requested_by=user.requested_by, acting_as=user.acting_as, is_admin=user.is_admin)

    @app.get("/api/me/tokens", response_model=list[ApiTokenOut], include_in_schema=False)
    async def list_tokens(
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> list[ApiTokenOut]:
        from vts.db.repo import Repo as _Repo
        repo = _Repo(session)
        rows = await repo.list_api_tokens(uuid.UUID(user.id))
        return [
            ApiTokenOut(
                id=r.id, name=r.name, prefix=r.prefix,
                created_at=r.created_at, last_used_at=r.last_used_at,
            )
            for r in rows
        ]

    @app.post(
        "/api/me/tokens",
        response_model=ApiTokenCreateOut,
        dependencies=[Depends(require_same_site)],
        include_in_schema=False,
    )
    async def create_token(
        payload: ApiTokenCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> ApiTokenCreateOut:
        from vts.db.repo import Repo as _Repo
        from vts.services.api_tokens import generate_token, hash_token, token_prefix
        raw = generate_token()
        repo = _Repo(session)
        row = await repo.create_api_token(
            user_id=uuid.UUID(user.id),
            name=payload.name.strip(),
            token_hash=hash_token(raw),
            prefix=token_prefix(raw),
        )
        await session.commit()
        return ApiTokenCreateOut(
            id=row.id, name=row.name, prefix=row.prefix,
            created_at=row.created_at, last_used_at=None, token=raw,
        )

    @app.delete(
        "/api/me/tokens/{token_id}",
        status_code=204,
        dependencies=[Depends(require_same_site)],
        include_in_schema=False,
    )
    async def revoke_token(
        token_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user_session_only),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        from vts.db.repo import Repo as _Repo
        repo = _Repo(session)
        ok = await repo.revoke_api_token(uuid.UUID(user.id), token_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Token not found")
        await session.commit()
        return Response(status_code=204)

    @app.get("/api/prompts", response_model=list[PromptOut])
    async def list_prompts_endpoint(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> list[PromptOut]:
        from vts.services.prompt_registry import list_system_prompts
        out: list[PromptOut] = [
            PromptOut(source="system", id=p.key, name=p.i18n_name_key, editable=False)
            for p in list_system_prompts()
        ]
        repo = Repo(session)
        for row in await repo.list_prompts(uuid.UUID(user.id)):
            out.append(PromptOut(source="user", id=str(row.id), name=row.name, editable=True))
        return out

    @app.post("/api/prompts", response_model=PromptOut)
    async def create_prompt_endpoint(
        payload: PromptCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PromptOut:
        repo = Repo(session)
        row = await repo.create_prompt(uuid.UUID(user.id), payload.name.strip(), payload.system_prompt)
        await session.commit()
        return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)

    @app.get("/api/prompts/system/{key}/text", response_model=SystemPromptTextOut)
    async def get_system_prompt_text_endpoint(
        key: str,
        user: AuthenticatedUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> SystemPromptTextOut:
        from vts.services.prompt_registry import list_system_prompts
        from vts.services.summarizer import load_prompt

        spec = next((p for p in list_system_prompts() if p.key == key), None)
        if spec is None:
            raise HTTPException(status_code=404, detail="System prompt not found")
        text = load_prompt(settings.prompts_dir, spec.file, "")
        return SystemPromptTextOut(system_prompt=text)

    @app.get("/api/prompts/{prompt_id}", response_model=PromptDetailOut)
    async def get_prompt_detail_endpoint(
        prompt_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PromptDetailOut:
        repo = Repo(session)
        row = await repo.get_prompt(uuid.UUID(user.id), prompt_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return PromptDetailOut(
            source="user",
            id=str(row.id),
            name=row.name,
            system_prompt=row.system_prompt,
            editable=True,
        )

    @app.patch("/api/prompts/{prompt_id}", response_model=PromptOut)
    async def update_prompt_endpoint(
        prompt_id: uuid.UUID,
        payload: PromptUpdateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PromptOut:
        repo = Repo(session)
        row = await repo.update_prompt(
            uuid.UUID(user.id), prompt_id,
            name=payload.name,  # validated + stripped by PromptUpdateRequest; None = unchanged
            system_prompt=payload.system_prompt,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Prompt not found")
        await session.commit()
        return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)

    @app.delete("/api/prompts/{prompt_id}", status_code=204)
    async def delete_prompt_endpoint(
        prompt_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        ok = await repo.delete_prompt(uuid.UUID(user.id), prompt_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Prompt not found")
        await session.commit()
        return Response(status_code=204)

    @app.get("/api/push/config", response_model=PushConfigOut, include_in_schema=False)
    async def push_config(settings: Settings = Depends(get_settings_dep)) -> PushConfigOut:
        if not is_push_enabled(settings):
            return PushConfigOut(enabled=False, public_key=None)
        return PushConfigOut(enabled=True, public_key=settings.vapid_public_key)

    @app.get("/api/push/status", response_model=PushStatusOut, include_in_schema=False)
    async def push_status(
        endpoint: str | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PushStatusOut:
        subs = await list_subscriptions(session, uuid.UUID(user.id))
        if endpoint:
            match = next((s for s in subs if s.endpoint == endpoint), None)
            return PushStatusOut(subscribed=match is not None, endpoint=endpoint if match else None)
        first = subs[0] if subs else None
        return PushStatusOut(subscribed=first is not None, endpoint=first.endpoint if first else None)

    @app.post("/api/push/subscribe", response_model=PushStatusOut, include_in_schema=False)
    async def push_subscribe(
        payload: PushSubscriptionIn,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        settings: Settings = Depends(get_settings_dep),
    ) -> PushStatusOut:
        if not is_push_enabled(settings):
            raise HTTPException(status_code=503, detail="Push notifications are not configured")
        await upsert_subscription(
            session,
            uuid.UUID(user.id),
            SubscriptionPayload(
                endpoint=payload.endpoint,
                p256dh=payload.p256dh,
                auth=payload.auth,
                user_agent=payload.user_agent,
            ),
        )
        return PushStatusOut(subscribed=True, endpoint=payload.endpoint)

    @app.post("/api/push/unsubscribe", response_model=PushStatusOut, include_in_schema=False)
    async def push_unsubscribe(
        payload: PushUnsubscribeIn,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PushStatusOut:
        await delete_subscription(session, payload.endpoint)
        return PushStatusOut(subscribed=False, endpoint=None)

    @app.get("/api/admin/users", response_model=AdminUsersOut)
    async def admin_users(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> AdminUsersOut:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        repo = Repo(session)
        users = await repo.list_usernames()
        return AdminUsersOut(users=users)

    @app.post("/api/tasks", response_model=TaskOut)
    async def create_task(
        request: TaskCreateRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        repo = Repo(session)
        effective_user_id = uuid.UUID(user.id)
        task_id = uuid.uuid4()
        artifact = task_dir(settings.artifacts_root, user.username, task_id)
        artifact.mkdir(parents=True, exist_ok=True)
        options = request.model_dump()
        options.pop("url", None)
        task = await repo.create_task(
            user_id=effective_user_id,
            source_url=request.url,
            options=options,
            artifact_dir=str(artifact),
            task_id=task_id,
        )
        await session.commit()
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
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.get("/api/tasks/{task_id}/results/{source}/{ref}", include_in_schema=False)
    async def get_prompt_result(
        task_id: uuid.UUID,
        source: str,
        ref: str,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> PlainTextResponse:
        repo = Repo(session)
        task = await repo.get_task_by_id(task_id)
        if task is None or str(task.user_id) != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        from vts.services.prompt_results import resolve_result_path
        path = resolve_result_path(task, source, ref)
        if path is None or not Path(path).exists():
            raise HTTPException(status_code=404, detail="Result not found")
        return PlainTextResponse(Path(path).read_text(encoding="utf-8"))

    _ALLOWED_UPLOAD_SUFFIXES = frozenset(
        {
            ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
            ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
        }
    )

    @app.post("/api/tasks/upload", response_model=TaskOut)
    async def upload_task(
        file: UploadFile = File(...),
        language: str | None = Form(default=None),
        display_name: str | None = Form(default=None),
        audio_only: bool = Form(default=False),
        transcript: bool = Form(default=True),
        prompts: str | None = Form(default=None),
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        from vts.services.prompt_registry import parse_ref, ref_to_dict
        if prompts is None:
            normalized_prompts = [{"source": "system", "id": "summary"}]
        else:
            try:
                raw_refs = json.loads(prompts)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail="prompts must be valid JSON") from exc
            if not isinstance(raw_refs, list):
                raise HTTPException(status_code=422, detail="prompts must be a JSON list")
            normalized_prompts = []
            for entry in raw_refs:
                try:
                    source, ref_id = parse_ref(entry)
                except (ValueError, TypeError) as exc:
                    raise HTTPException(status_code=422, detail=f"invalid prompt ref: {entry!r}") from exc
                normalized_prompts.append(ref_to_dict(source, ref_id))
        if normalized_prompts and not transcript:
            raise HTTPException(status_code=422, detail="prompts require transcript")
        original_filename = file.filename or "upload"
        suffix = Path(original_filename).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix or '(none)'}")

        repo = Repo(session)
        effective_user_id = uuid.UUID(user.id)
        task_id = uuid.uuid4()
        artifact = task_dir(settings.artifacts_root, user.username, task_id)
        artifact.mkdir(parents=True, exist_ok=True)
        media_dir = artifact / "media"
        media_dir.mkdir(exist_ok=True)

        safe_name = "audio.original" + suffix
        dest = media_dir / safe_name
        content = await file.read()
        await asyncio.to_thread(dest.write_bytes, content)

        source_url = f"file://{Path(original_filename).name}"
        options = {
            "language": language or None,
            "audio_only": audio_only,
            "transcript": transcript,
            "prompts": normalized_prompts,
        }
        task = await repo.create_task(
            user_id=effective_user_id,
            source_url=source_url,
            options=options,
            artifact_dir=str(artifact),
            task_id=task_id,
            source_title=normalize_display_name(display_name),
        )
        await session.commit()
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
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.get(
        "/api/tasks",
        response_model=list[TaskOut] | list[TaskCompactOut],
    )
    async def list_tasks(
        limit: int | None = None,
        offset: int = 0,
        compact: bool = False,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> list[TaskOut] | list[TaskCompactOut]:
        """List tasks owned by the current user, newest first. Use
        `limit`/`offset` to paginate and `compact=true` for slim records
        (no steps/options/paths) when the client has a small response
        budget (e.g. ChatGPT Custom Actions cap at ~30KB)."""
        if limit is not None and limit < 0:
            raise HTTPException(status_code=422, detail="limit must be non-negative")
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        if limit is not None and limit > 500:
            raise HTTPException(status_code=422, detail="limit must be <= 500")
        repo = Repo(session)
        tasks = await repo.list_tasks_for_user(
            uuid.UUID(user.id), limit=limit, offset=offset,
        )
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        task_ids = [task.id for task in tasks]
        asr_progress = await repo.get_asr_progress_for_tasks(task_ids)
        summary_progress = {task.id: summary_progress_for_task(task) for task in tasks}
        if compact:
            return [serialize_task_compact(task, queue_positions, asr_progress, summary_progress) for task in tasks]
        return [serialize_task(task, queue_positions, asr_progress, summary_progress) for task in tasks]

    @app.get("/api/tasks/queue-positions", include_in_schema=False)
    async def get_queue_positions(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> JSONResponse:
        repo = Repo(session)
        positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        return JSONResponse({str(k): v for k, v in positions.items()})

    @app.get("/api/tasks/{task_id}", response_model=TaskOut)
    async def get_task(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.patch("/api/tasks/{task_id}", response_model=TaskOut)
    async def update_task(
        task_id: uuid.UUID,
        payload: TaskUpdate,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        task.source_title = normalize_display_name(payload.display_name)
        await session.commit()
        queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
        asr_progress = await repo.get_asr_progress_for_tasks([task.id])
        summary_progress = {task.id: summary_progress_for_task(task)}
        return serialize_task(task, queue_positions, asr_progress, summary_progress)

    @app.post("/api/tasks/restart_summary", response_model=BatchResultOut)
    async def restart_summary_tasks(
        request: RestartSummaryRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids, load_steps=True)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        bus = RedisBus(redis, settings)
        artifact_resets: list[asyncio.Task[None]] = []
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            if request.mode == "final_only":
                if not can_restart_final_summary_task(task):
                    results[tid] = f"cannot_restart_final:{task.status.value}"
                    continue
                if request.prompts is not None:
                    # New-set restart: swap options.prompts, clear all finalize
                    # results, rebuild the finalize tail, re-queue.
                    from vts.services.prompt_results import clear_all_finalize_results

                    new_refs = [
                        {"source": p.source, "id": p.id} for p in request.prompts
                    ]
                    clear_all_finalize_results(task)  # files, prompt_results, summary_path
                    new_options = dict(task.options or {})
                    new_options["prompts"] = new_refs
                    task.options = new_options
                    await _rebuild_finalize_tail(repo, task, new_options)
                else:
                    _reset_final_summary_step(task)
                    artifact_resets.append(asyncio.to_thread(_reset_final_summary_artifacts, task))
            else:
                if not can_restart_summary_task(task):
                    results[tid] = f"cannot_restart:{task.status.value}"
                    continue
                _reset_summary_steps(task)
                artifact_resets.append(asyncio.to_thread(_reset_summary_artifacts, task))
            task.summary_path = None
            await repo.set_task_summary_progress(task, 0, 0)
            await repo.set_task_status(task, TaskStatus.queued)
            results[tid] = "queued"
        await asyncio.gather(*artifact_resets)
        await session.commit()
        if any(v == "queued" for v in results.values()):
            await bus.notify_queued()
        return BatchResultOut(results=results)

    @app.post("/api/tasks/pause", response_model=BatchResultOut)
    async def pause_tasks(
        request: TaskIdsRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        bus = RedisBus(redis, settings)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            if not can_pause_task(task.status):
                results[tid] = f"cannot_pause:{task.status.value}"
                continue
            await repo.set_task_status(task, TaskStatus.paused)
            await bus.request_pause(task_id)
            results[tid] = "paused"
        await session.commit()
        return BatchResultOut(results=results)

    @app.post("/api/tasks/resume", response_model=BatchResultOut)
    async def resume_tasks(
        request: TaskIdsRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        bus = RedisBus(redis, settings)
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            if not can_resume_task(task.status):
                results[tid] = f"cannot_resume:{task.status.value}"
                continue
            await bus.clear_pause_request(task_id)
            await repo.set_task_status(task, TaskStatus.queued)
            results[tid] = "queued"
        await session.commit()
        if any(v == "queued" for v in results.values()):
            await bus.notify_queued()
        return BatchResultOut(results=results)

    @app.delete("/api/tasks", response_model=BatchResultOut)
    async def delete_tasks(
        request: TaskIdsRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        bus = RedisBus(redis, settings)
        artifacts_to_remove: list[Path] = []
        tasks_to_delete: list = []
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            tasks_to_delete.append(task)
            results[tid] = "deleted"
        if tasks_to_delete:
            await asyncio.gather(
                *[bus.request_cancel(t.id) for t in tasks_to_delete],
            )
            for task in tasks_to_delete:
                await repo.set_task_status(task, TaskStatus.canceled)
                artifacts_to_remove.append(Path(task.artifact_dir))
                await session.delete(task)
        await session.commit()
        await asyncio.gather(
            *[asyncio.to_thread(shutil.rmtree, artifact, True) for artifact in artifacts_to_remove]
        )
        return BatchResultOut(results=results)

    @app.post("/api/tasks/archive", response_model=BatchResultOut)
    async def archive_tasks(
        request: TaskIdsRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> BatchResultOut:
        repo = Repo(session)
        tasks = await repo.get_tasks_for_user(uuid.UUID(user.id), request.task_ids)
        task_map = {task.id: task for task in tasks}
        results: dict[str, str] = {}
        for task_id in request.task_ids:
            tid = str(task_id)
            task = task_map.get(task_id)
            if task is None:
                results[tid] = "not_found"
                continue
            if task.status not in {TaskStatus.completed, TaskStatus.failed}:
                results[tid] = f"cannot_archive:{task.status.value}"
                continue
            await asyncio.to_thread(_archive_task_artifacts, task)
            await repo.set_task_status(task, TaskStatus.archived)
            results[tid] = "archived"
        await session.commit()
        return BatchResultOut(results=results)

    @app.get(
        "/api/tasks/{task_id}/transcript",
        responses={
            200: {
                "description": (
                    "Raw transcript. Default response is text/plain (full body). "
                    "With Accept: application/json or ?offset/limit query, returns a "
                    "TextSliceOut JSON. With a `Range: bytes=START-END` header, returns "
                    "206 Partial Content. See docs/API.md for the rationale."
                ),
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
                },
            },
            206: {"description": "Partial transcript (Range request)"},
            404: {"description": "Task or transcript artifact not found"},
        },
    )
    async def get_transcript(
        task_id: uuid.UUID,
        request: Request,
        offset: int | None = None,
        limit: int | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.transcript_path:
            raise HTTPException(status_code=404, detail="Transcript is not ready")
        path = Path(task.transcript_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Transcript file missing")
        plain_mt = "text/plain; charset=utf-8" if path.suffix == ".txt" else "application/json"
        return _serve_text(
            path.read_text(encoding="utf-8"),
            plain_mt,
            request=request,
            offset=offset,
            limit=limit,
        )

    @app.get(
        "/api/tasks/{task_id}/summary",
        responses={
            200: {
                "description": (
                    "Markdown summary. Default response is text/markdown (full body). "
                    "With Accept: application/json or ?offset/limit, returns TextSliceOut. "
                    "With Range header, returns 206 Partial Content."
                ),
                "content": {
                    "text/markdown": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
                },
            },
            206: {"description": "Partial summary (Range request)"},
            404: {"description": "Task or summary artifact not found"},
        },
    )
    async def get_summary(
        task_id: uuid.UUID,
        request: Request,
        offset: int | None = None,
        limit: int | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.summary_path:
            raise HTTPException(status_code=404, detail="Summary is not ready")
        path = Path(task.summary_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Summary file missing")
        plain_mt = "text/markdown; charset=utf-8" if path.suffix in {".md", ".markdown"} else "application/json"
        return _serve_text(
            path.read_text(encoding="utf-8"),
            plain_mt,
            request=request,
            offset=offset,
            limit=limit,
        )

    @app.get(
        "/api/tasks/{task_id}/redacted",
        responses={
            200: {
                "description": (
                    "Redacted plain-text transcript. Supports the same paginated "
                    "modes as /transcript (Accept: application/json or Range header)."
                ),
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
                },
            },
            206: {"description": "Partial redacted transcript (Range request)"},
            404: {"description": "Task or redacted transcript not found"},
        },
    )
    async def get_redacted_transcript(
        task_id: uuid.UUID,
        request: Request,
        offset: int | None = None,
        limit: int | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
        return _serve_text(
            path.read_text(encoding="utf-8"),
            "text/plain; charset=utf-8",
            request=request,
            offset=offset,
            limit=limit,
        )

    @app.get(
        "/api/tasks/{task_id}/log",
        responses={
            200: {
                "description": (
                    "Plain-text task log. Empty body if the task has no log yet. "
                    "Supports the same paginated modes as /transcript."
                ),
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                    "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
                },
            },
            206: {"description": "Partial log (Range request)"},
            404: {"description": "Task not found"},
        },
    )
    async def get_log(
        task_id: uuid.UUID,
        request: Request,
        offset: int | None = None,
        limit: int | None = None,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        path = Path(task.artifact_dir) / "logs" / "task.log"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return _serve_text(
            text,
            "text/plain; charset=utf-8",
            request=request,
            offset=offset,
            limit=limit,
        )

    @app.get("/api/tasks/{task_id}/media")
    async def get_media(
        task_id: uuid.UUID,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> FileResponse:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        media_file = _find_media_file(task.artifact_dir)
        if media_file is None:
            raise HTTPException(status_code=404, detail="Media file not available")
        return FileResponse(
            path=str(media_file),
            filename=media_file.name,
            media_type=media_content_type(media_file),
        )

    @app.get("/player/{task_id}", include_in_schema=False, response_class=HTMLResponse)
    async def media_player(
        task_id: uuid.UUID,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> HTMLResponse:
        repo = Repo(session)
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        media_file = _find_media_file(task.artifact_dir)
        if media_file is None:
            raise HTTPException(status_code=404, detail="Media file not available")
        kind = media_kind(media_file)
        # source_url is "file://<name>" for uploads, an http URL otherwise;
        # in either case the last path segment is a sensible display name.
        title = (task.source_url or "").rsplit("/", 1)[-1] or media_file.name
        # Propagate admin impersonation: <video>/<audio> will fire its own
        # request to /api/tasks/<id>/media, which must resolve to the same
        # acting user as the page itself — otherwise the request resolves
        # as the admin and the task ownership check returns 404.
        src = f"/api/tasks/{task_id}/media"
        acting_as = request.query_params.get("as_user")
        if acting_as:
            src = f"{src}?{urlencode({'as_user': acting_as})}"
        tag = (
            f'<video controls autoplay src="{_html.escape(src, quote=True)}"></video>'
            if kind == "video"
            else f'<audio controls autoplay src="{_html.escape(src, quote=True)}"></audio>'
        )
        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #111; color: #ddd;
    font-family: system-ui, sans-serif; min-height: 100vh; }}
  body {{ display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 1rem; }}
  h1 {{ font-size: 1rem; font-weight: 400; margin: 0 0 1rem;
    word-break: break-all; text-align: center; }}
  video, audio {{ max-width: 100%; width: min(960px, 100%); }}
  video {{ max-height: 80vh; background: #000; }}
</style>
</head>
<body>
<h1>{_html.escape(title)}</h1>
{tag}
</body>
</html>"""
        return HTMLResponse(html)

    @app.get("/api/events", include_in_schema=False)
    async def get_events(
        user: AuthenticatedUser = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> StreamingResponse:
        async def event_generator() -> Any:
            yield f"event: server_version\ndata: {json.dumps({'version': __version__}, ensure_ascii=True)}\n\n"
            pubsub = redis.pubsub()
            channel = f"{settings.redis_prefix}events"
            await pubsub.subscribe(channel)
            try:
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
                    if not message:
                        yield "event: ping\ndata: {}\n\n"
                        continue
                    data = json.loads(message["data"].decode("utf-8"))
                    if data.get("user_id") != user.id:
                        continue
                    yield f"event: {data.get('event', 'message')}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"
            finally:
                await pubsub.unsubscribe(channel)
                await pubsub.close()

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return app


app = create_app()
