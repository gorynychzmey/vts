"""Re-attribute the transcripts of diarized tasks to speakers.

A task's transcript is merged once, when the task runs: each word goes to the
speaker diarization heard at that moment. If the word times were wrong at that
point, the transcript stays wrong after the reading code is fixed. That
happened with whisper.cpp under --vad, which reported words seconds before
they were said (see `realign_word_times` in vts/services/asr_payload.py), so
whole answers ended up under the other speaker's name.

This repeats the merge from the stored ASR segments and diarization, then
re-applies what the user decided since (names, speakers marked as noise) and
re-indexes the recording for search. Summaries are left alone.

    python scripts/remerge_transcripts.py --all-diarized
    python scripts/remerge_transcripts.py <task-id> [<task-id> ...] --show 3
    python scripts/remerge_transcripts.py --all-diarized --commit

Without --commit it reports, per task, how many words would change speaker and
writes nothing. With --commit the previous transcript.json/.txt are kept next
to the new ones as *.pre-remerge (once; a second run does not overwrite them).

Run it where the application runs — inside its container — since the artifact
paths stored in the database are paths there.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from vts.core.config import get_settings
from vts.db.models import Task
from vts.db.repo import Repo
from vts.db.session import SessionLocal
from vts.pipeline.rerender import rerender_transcript
from vts.pipeline.steps.transcription import apply_diarization, effective_language
from vts.services.asr_payload import segment_raw_payload
from vts.services.indexing import reindex_task
from vts.services.storage import write_json_atomic

BACKUP_SUFFIX = ".pre-remerge"


def _words(entries: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(w, e.get("speaker")) for e in entries for w in str(e.get("text", "")).split()]


def _changed_words(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> tuple[int, int, list[tuple[int, int]]]:
    """(words compared, words whose speaker changed, changed spans in `new`)."""
    a, b = _words(old), _words(new)
    matcher = difflib.SequenceMatcher(a=[w for w, _ in a], b=[w for w, _ in b], autojunk=False)
    compared = changed = 0
    spans: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            compared += 1
            if a[block.a + k][1] != b[block.b + k][1]:
                changed += 1
                position = block.b + k
                if spans and spans[-1][1] == position:
                    spans[-1] = (spans[-1][0], position + 1)
                else:
                    spans.append((position, position + 1))
    return compared, changed, spans


def _excerpt(words: list[tuple[str, Any]], start: int, end: int, names: dict[str, str]) -> str:
    lo, hi = max(0, start - 8), min(len(words), end + 8)
    out: list[str] = []
    current = object()
    for word, speaker in words[lo:hi]:
        if speaker != current:
            out.append(f"\n      [{names.get(speaker, speaker)}]")
            current = speaker
        out.append(word)
    return " ".join(out)


async def _merge(repo: Repo, task: Task, settings: Any) -> tuple[dict[str, Any], str | None] | None:
    """The transcript payload MergeTranscriptStep would write today, or None
    when the task has no usable diarization. Mirrors that step: change both
    together."""
    outputs = Path(task.artifact_dir) / "outputs"
    entries: list[dict[str, Any]] = []
    merged_tokens: list[str] = []
    raw_json_by_index: dict[int, dict[str, Any]] = {}
    for segment in await repo.get_task_segments(task.id):
        text = segment.text.strip()
        if not text:
            continue
        merged_tokens.append(text)
        payload = segment_raw_payload(segment)
        if payload:
            raw_json_by_index[len(entries)] = payload
        entries.append({"start": segment.start_sec, "end": segment.end_sec, "text": text})

    language = effective_language(task.options or {}, {"outputs": outputs})
    names = await repo.speaker_names_for_task(task.user_id, task.id)
    merged, text, cleanup = apply_diarization(
        entries,
        raw_json_by_index,
        outputs / "diarization.json",
        min_words=int(getattr(settings, "diarization_min_words", 2)),
        min_seconds=float(getattr(settings, "diarization_min_seconds", 0.8)),
        min_share=0.0,
        language=language,
        names=names,
    )
    if text is None:
        return None
    return (
        {
            "text": text,
            "raw_text": " ".join(merged_tokens).strip(),
            "entries": merged,
            "all_entries": merged,
            "cleanup": cleanup,
        },
        language,
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with SessionLocal() as session:
        repo = Repo(session)
        if args.all_diarized:
            rows = (await session.scalars(select(Task).where(Task.artifact_dir.is_not(None)))).all()
            tasks = [t for t in rows if (Path(t.artifact_dir) / "outputs" / "diarization.json").exists()]
        else:
            tasks = []
            for raw_id in args.task_ids:
                task = await repo.get_task_by_id(uuid.UUID(raw_id))
                if task is None:
                    print(f"{raw_id}: no such task")
                    continue
                tasks.append(task)

        for task in sorted(tasks, key=lambda t: t.created_at):
            outputs = Path(task.artifact_dir) / "outputs"
            transcript_json = outputs / "transcript.json"
            try:
                current = json.loads(transcript_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(f"{task.id}: no readable transcript.json, skipped")
                continue
            result = await _merge(repo, task, settings)
            if result is None:
                print(f"{task.id}: no usable diarization, skipped")
                continue
            payload, language = result
            old_entries = current.get("all_entries") or current.get("entries") or []
            compared, changed, spans = _changed_words(old_entries, payload["all_entries"])
            share = changed / compared if compared else 0.0
            print(f"{task.id}: {changed} of {compared} words change speaker ({share:.1%})")

            if args.show:
                names = await repo.speaker_names_for_task(task.user_id, task.id)
                old_words, new_words = _words(old_entries), _words(payload["all_entries"])
                matcher = difflib.SequenceMatcher(
                    a=[w for w, _ in old_words], b=[w for w, _ in new_words], autojunk=False
                )
                for start, end in sorted(spans, key=lambda s: s[0] - s[1])[: args.show]:
                    old_start = next(
                        (blk.a + (start - blk.b) for blk in matcher.get_matching_blocks()
                         if blk.b <= start < blk.b + blk.size),
                        start,
                    )
                    print(f"   before:{_excerpt(old_words, old_start, old_start + end - start, names)}")
                    print(f"   after: {_excerpt(new_words, start, end, names)}\n")

            if not args.commit or changed == 0:
                continue
            for name in ("transcript.json", "transcript.txt"):
                source = outputs / name
                backup = outputs / (name + BACKUP_SUFFIX)
                if source.exists() and not backup.exists():
                    shutil.copy2(source, backup)
            write_json_atomic(transcript_json, payload)
            (outputs / "transcript.txt").write_text(payload["text"], encoding="utf-8")
            # Names and noise marks the user set since the first merge.
            await rerender_transcript(task, session, language=language)
            await reindex_task(session, task, settings)
            await session.commit()
            print("   written")

    if not args.commit:
        print("DRY RUN — nothing written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_ids", nargs="*")
    parser.add_argument("--all-diarized", action="store_true", help="every task with a diarization artifact")
    parser.add_argument("--show", type=int, default=0, metavar="N", help="print the N largest changed passages per task")
    parser.add_argument("--commit", action="store_true", help="write the result; without it nothing is written")
    args = parser.parse_args()
    if not args.task_ids and not args.all_diarized:
        parser.error("give task ids or --all-diarized")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
