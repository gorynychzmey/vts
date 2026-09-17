"""The read-only guard on the production query helper (vts-zezg).

`scripts/prodq.py` exists to stop a nine-line async boilerplate being retyped
for every one-line question about the database — measured at 32 repetitions in
a single day, each one re-inventing the quoting for SQL string literals inside
`python3 -c "..."`, which is precisely where a silent mistake hides.

Convenience aimed at PRODUCTION lowers the threshold for running the wrong
thing there, which is the objection raised when the helper was proposed. So the
guard is the part worth testing: refuse anything that is not a plain read
unless the caller says otherwise in as many words.
"""
from __future__ import annotations

import pytest

from scripts.prodq import RefusedWrite, ensure_read_only


@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM tasks",
    "  select 1  ",
    "\n-- a leading comment\nSELECT 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "TABLE tasks",
    "EXPLAIN SELECT * FROM tasks",
    "SHOW server_version",
])
def test_reads_are_allowed(sql):
    ensure_read_only(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM tasks",
    "UPDATE tasks SET status='x'",
    "INSERT INTO tasks VALUES (1)",
    "DROP TABLE tasks",
    "TRUNCATE tasks",
    "ALTER TABLE tasks ADD COLUMN x int",
    "CREATE INDEX foo ON tasks (id)",
    "GRANT ALL ON tasks TO someone",
    "VACUUM FULL tasks",
])
def test_writes_are_refused(sql):
    with pytest.raises(RefusedWrite):
        ensure_read_only(sql)


def test_a_write_hidden_behind_a_leading_read_is_refused():
    # The check must look at every statement, not just the first word: a
    # semicolon is all it takes to append the dangerous half.
    with pytest.raises(RefusedWrite):
        ensure_read_only("SELECT 1; DROP TABLE tasks")


def test_a_write_inside_a_cte_is_refused():
    # Postgres allows data-modifying CTEs, so "WITH" is not proof of a read.
    with pytest.raises(RefusedWrite):
        ensure_read_only("WITH d AS (DELETE FROM tasks RETURNING id) SELECT * FROM d")


def test_a_keyword_inside_a_string_literal_does_not_trigger_a_refusal():
    # Refusing a legitimate read because the word appears in a value would push
    # the user straight back to hand-rolled boilerplate — the thing this helper
    # exists to replace.
    ensure_read_only("SELECT * FROM tasks WHERE source_title = 'how to DELETE a file'")


def test_a_keyword_inside_a_comment_does_not_trigger_a_refusal():
    ensure_read_only("SELECT 1 -- DROP TABLE tasks\n")


def test_an_empty_statement_is_refused_rather_than_run():
    with pytest.raises(RefusedWrite):
        ensure_read_only("   ")


# ------------------------------- functions that write while looking like reads

@pytest.mark.parametrize("sql", [
    "SELECT setval('tasks_id_seq', 1)",
    "SELECT nextval('tasks_id_seq')",
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
    "SELECT lo_unlink(1)",
    "SELECT pg_cancel_backend(123)",
])
def test_writing_functions_wrapped_in_a_select_are_refused(sql):
    """Keyword matching cannot see through a function call (vts-p54i).

    `SELECT setval(...)` starts with SELECT and contains no write KEYWORD, so
    the allowlist waves it through — and it writes. So does
    `pg_terminate_backend`, which kills other people's sessions on the
    production database.

    The set of writing functions is open-ended, so a longer denylist would only
    postpone the problem. These cases are pinned because they are the ones that
    were demonstrated, but the defence is the read-only TRANSACTION below:
    Postgres itself refuses the write, whatever it is called.
    """
    with pytest.raises(RefusedWrite):
        ensure_read_only(sql)


def test_the_statement_runs_inside_a_read_only_transaction():
    """The real guarantee, and it does not depend on parsing SQL at all.

    A keyword gate can only refuse what it recognises. `SET TRANSACTION READ
    ONLY` makes the database refuse every write, including the ones through
    functions nobody thought to list.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "prodq.py").read_text(
        encoding="utf-8"
    )
    assert "READ ONLY" in source, (
        "the script does not open a read-only transaction; the keyword gate is "
        "the only defence, and it cannot see through function calls"
    )


def test_the_refusal_explains_itself_without_claiming_a_write():
    """vts-6wvy: three entries on the list do not modify data.

    `pg_advisory_lock`, `pg_advisory_unlock` and `query_to_xml` were refused
    with "it modifies the database even inside a SELECT", which is false for
    all three — and a false reason is worse than a terse one, because the next
    reader checks the claim, finds it wrong, and removes the entry.

    They stay refused, and the criterion is not "writes data". It is "has an
    effect the keyword scan cannot see":

    * an advisory lock is session-scoped and held until released, so it can
      block the application on the production database — READ ONLY does not
      forbid it;
    * `query_to_xml` takes a query as a STRING, so the inner statement is
      invisible to the scan above; it is the standard way to smuggle one past
      a word-level filter.
    """
    for sql in (
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_advisory_unlock(1)",
        "SELECT query_to_xml('UPDATE tasks SET source_title = 1', true, true, '')",
    ):
        with pytest.raises(RefusedWrite) as exc:
            ensure_read_only(sql)
        assert "modifies the database" not in str(exc.value), (
            f"the refusal of {sql!r} claims a write that does not happen"
        )
        # It must still say enough to act on.
        assert "--write" in str(exc.value) or "refusing" in str(exc.value)


@pytest.mark.parametrize("sql", [
    "SELECT pg_advisory_lock_shared(1)",
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_try_advisory_xact_lock(1)",
    "SELECT pg_advisory_xact_lock_shared(1)",
    "SELECT pg_advisory_unlock_all()",
])
def test_the_whole_advisory_lock_family_is_refused(sql):
    """The scan matches WHOLE words, so each variant is its own name.

    `re.findall(r"[a-zA-Z_]+", …)` reads `pg_advisory_lock_shared` as one word,
    which is not `pg_advisory_lock` — so listing the two plain functions left
    ten siblings walking straight through, all of them able to take a lock that
    blocks the application and that READ ONLY does not forbid. Matched by
    prefix now, which is what "the advisory family" actually means.
    """
    with pytest.raises(RefusedWrite):
        ensure_read_only(sql)


def test_a_read_that_merely_mentions_advisory_is_still_allowed():
    """The prefix must apply to a FUNCTION word, not to any text.

    Refusing a legitimate read because a column or a value looks like a lock
    name would push the user back to hand-rolled boilerplate — the thing this
    script replaces.
    """
    ensure_read_only("SELECT * FROM tasks WHERE source_title = 'pg_advisory_lock'")
    ensure_read_only("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
