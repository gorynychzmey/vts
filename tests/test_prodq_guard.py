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
