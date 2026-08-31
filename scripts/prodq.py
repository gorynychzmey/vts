#!/usr/bin/env python3
"""Ask the production database one question, without retyping the boilerplate.

Every ad-hoc query used to be a nine-line async preamble inside
`python3 -c "..."`, re-typed for a single line of SQL — 32 times in one day
(vts-zezg). Beyond the noise, the quoting for SQL string literals had to be
rebuilt each time (`text(\"... where id='...'\")` inside a double-quoted shell
argument), which is exactly where a mistake passes unnoticed.

**Read-only unless told otherwise.** A convenient one-liner aimed at production
lowers the threshold for running the wrong thing there, so anything that is not
a plain read is refused. `--write` lifts that, and has to be typed out: the
point is that a destructive statement cannot happen by reflex.

Usage:
    python scripts/prodq.py 'SELECT count(*) FROM tasks'
    python scripts/prodq.py --write 'UPDATE ...'      # deliberate, and visible
    python scripts/prodq.py --json 'SELECT ...'

The connection comes from the same Settings the application uses, so it obeys
config.yaml rather than carrying a second copy of the DSN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

# Statements that only read. Anything outside this set is refused by default —
# an allowlist, because the set of ways to modify a database is open-ended and
# a denylist would keep growing.
_READ_ONLY_STARTS = frozenset({"select", "with", "table", "values", "explain", "show"})

# Keywords that modify, checked against every statement including the inside of
# a CTE: Postgres allows data-modifying CTEs, so a leading WITH proves nothing.
# Functions that WRITE while sitting inside a plain SELECT. Keyword matching
# cannot see them: `SELECT setval(...)` starts with SELECT and contains no write
# keyword (vts-p54i). The list is not, and cannot be, complete — the real
# defence is the read-only TRANSACTION in _run; this exists so the common cases
# are refused before a connection is even opened, with a message that says why.
_WRITE_FUNCTIONS = frozenset({
    "setval", "nextval",
    "pg_terminate_backend", "pg_cancel_backend",
    "lo_unlink", "lo_import", "lo_export", "lo_create",
    "pg_drop_replication_slot", "pg_create_physical_replication_slot",
    "pg_create_logical_replication_slot", "pg_replication_origin_create",
    "pg_import_system_collations", "pg_stat_reset", "pg_stat_statements_reset",
    "pg_switch_wal", "pg_advisory_lock", "pg_advisory_unlock",
    "dblink_exec", "query_to_xml",
})

_WRITE_KEYWORDS = frozenset({
    "insert", "update", "delete", "drop", "truncate", "alter", "create",
    "grant", "revoke", "vacuum", "reindex", "cluster", "copy", "call", "do",
    "refresh", "comment", "security", "set", "reset", "lock", "merge",
})


class RefusedWrite(RuntimeError):
    """The statement was not a plain read and --write was not given."""


def _strip_literals_and_comments(sql: str) -> str:
    """SQL with string literals and comments blanked out.

    Keyword matching has to ignore both, or a perfectly good read is refused
    because a value happens to contain the word DELETE — and being refused for
    no reason sends the user straight back to the hand-rolled boilerplate this
    script replaces.
    """
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    without_line = re.sub(r"--[^\n]*", " ", without_block)
    without_single = re.sub(r"'(?:[^']|'')*'", "''", without_line)
    return re.sub(r'"(?:[^"]|"")*"', '""', without_single)


def ensure_read_only(sql: str) -> None:
    """Raise RefusedWrite unless every statement is a plain read."""
    cleaned = _strip_literals_and_comments(sql)
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    if not statements:
        raise RefusedWrite("no statement to run")
    for statement in statements:
        words = re.findall(r"[a-zA-Z_]+", statement)
        if not words:
            raise RefusedWrite("no statement to run")
        if words[0].lower() not in _READ_ONLY_STARTS:
            raise RefusedWrite(
                f"refusing to run a non-read statement without --write: "
                f"{statement[:60]!r}"
            )
        # Every word, not just the first: a data-modifying CTE hides the verb
        # in the middle, and a semicolon appends a second statement.
        for word in words:
            lowered = word.lower()
            if lowered in _WRITE_KEYWORDS:
                raise RefusedWrite(
                    f"refusing to run a statement containing {word.upper()} "
                    f"without --write"
                )
            if lowered in _WRITE_FUNCTIONS:
                raise RefusedWrite(
                    f"refusing to run {word}(): it modifies the database even "
                    f"inside a SELECT"
                )


async def _run(sql: str, as_json: bool, allow_write: bool = False) -> int:
    # Run from anywhere: the repo root goes on the path the same way
    # scripts/gen_ui_inventory.py does it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from sqlalchemy import text

    from vts.core.config import get_settings
    from vts.db.session import SessionLocal

    settings = get_settings()
    async with SessionLocal() as session:
        if not allow_write:
            # The guarantee the keyword gate cannot give. A gate can only refuse
            # what it recognises, and writing functions are an open set
            # (vts-p54i); here Postgres itself rejects every write, whatever it
            # is called or however it is nested.
            await session.execute(text("SET TRANSACTION READ ONLY"))
        result = await session.execute(text(sql))
        try:
            rows = result.fetchall()
        except Exception:
            # A statement that returns nothing (only reachable with --write).
            print("(no rows returned)", file=sys.stderr)
            return 0
        if as_json:
            keys = list(result.keys())
            print(json.dumps(
                [dict(zip(keys, (str(v) for v in row))) for row in rows],
                ensure_ascii=False, indent=2,
            ))
        else:
            for row in rows:
                print("\t".join("" if v is None else str(v) for v in row))
            print(f"({len(rows)} rows)", file=sys.stderr)
    _ = settings
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("sql", help="the statement to run")
    parser.add_argument(
        "--write", action="store_true",
        help="allow a statement that modifies data (say so deliberately)",
    )
    parser.add_argument("--json", action="store_true", help="print rows as JSON")
    args = parser.parse_args(argv)

    if not args.write:
        try:
            ensure_read_only(args.sql)
        except RefusedWrite as exc:
            print(f"prodq: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_run(args.sql, args.json, allow_write=args.write))


if __name__ == "__main__":
    raise SystemExit(main())
