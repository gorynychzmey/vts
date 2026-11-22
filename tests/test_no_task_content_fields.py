"""Gate: the recording is the only home of a task's content (vts-lzad).

After stage 3 nothing in `vts/` reads or writes the task's content fields —
`transcript_path`, `summary_path` and `options["prompt_results"]` — except the
code that migrates old rows. A new read would quietly return NULL (the columns
are no longer written) and a new write would fork the content again, so both
fail here rather than in production.

The patterns target task-ish objects only: a `Recording` has attributes of the
same names and reading those is the point.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "vts"

# Files allowed to touch the old fields, each with its reason.
ALLOWED = {
    # Migration-time backfills read the old task columns by design.
    "vts/services/recording_backfill.py",
}

_TASKISH = r"(?:task|task_row|donor|t|Task)"
_FIELD = r"(?:transcript_path|summary_path)"
PATTERNS = [
    re.compile(rf"\b{_TASKISH}\.{_FIELD}\b"),
    re.compile(rf"getattr\(\s*{_TASKISH}\s*,\s*[\"']{_FIELD}[\"']"),
    re.compile(r"options[^\n]*\[\s*[\"']prompt_results[\"']\s*\]"),
    re.compile(r"options[^\n]*\.get\(\s*[\"']prompt_results[\"']"),
]


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(p.search(line) for p in PATTERNS):
                found.append(f"{rel}:{lineno}: {stripped}")
    return found


def test_nothing_outside_the_allow_list_touches_task_content_fields() -> None:
    offenders = _offenders()
    assert offenders == [], "task content fields used outside the allow-list:\n" + "\n".join(offenders)


def test_the_patterns_catch_the_shapes_they_are_for() -> None:
    caught = [
        "if not task.transcript_path:",
        "x = donor.summary_path",
        "t.transcript_path = None",
        "getattr(task, 'summary_path', None)",
        'entries = task.options["prompt_results"]',
        'pr = (donor.options or {}).get("prompt_results")',
        "select(Task.summary_path)",
    ]
    passed = [
        "recording.transcript_path",
        "rec.summary_path = None",
        "getattr(recording, 'summary_path', None)",
        "recording.prompt_results",
    ]
    for line in caught:
        assert any(p.search(line) for p in PATTERNS), line
    for line in passed:
        assert not any(p.search(line) for p in PATTERNS), line


def test_allow_listed_files_exist() -> None:
    for rel in ALLOWED:
        assert (ROOT / rel).is_file(), rel
