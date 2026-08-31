"""Paging and the honest total in corpus search.

The bug these guard against does not raise: the search simply returns fewer
passages than qualify, so a caller concludes "that is everything" from a
truncated scan. Measured on production before the fix: 998 passages cleared
the threshold and `limit=100` returned 37.
"""
from __future__ import annotations

import pytest

from vts.services import corpus_search


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else 0


class _Row:
    def __init__(self, i, score):
        self.id = i
        self.recording_id = i
        self.source_task_id = None
        self.title = f"rec-{i}"
        self.text = f"passage {i}"
        self.start_sec = float(i)
        self.end_sec = float(i) + 1.0
        self.speakers = []
        self.score = score


class _FakeSession:
    """Records the statements issued, and serves ranked rows."""

    def __init__(self, scores):
        self.scores = scores
        self.statements: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        if "count(*)" in sql:
            n = sum(1 for s in self.scores if s >= (params or {}).get("threshold", 0))
            return _FakeResult([(n,)])
        fetch = (params or {}).get("fetch", len(self.scores))
        rows = [_Row(i, s) for i, s in enumerate(self.scores)][:fetch]
        return _FakeResult(rows)


@pytest.mark.asyncio
async def test_index_scan_is_widened_before_searching():
    """Without this the HNSW scan stops early and the page comes back short."""
    session = _FakeSession([0.9] * 10)
    await corpus_search.search_chunks(session, __import__("uuid").uuid4(), [0.1] * 4, limit=5)
    issued = " ".join(session.statements)
    assert "hnsw.iterative_scan" in issued, "the scan was not widened"
    assert "strict_order" in issued, (
        "relaxed order would break the threshold cut, which stops at the first "
        "row below the bar and assumes the rest are below it too"
    )
    assert "SET LOCAL" in issued, "must not leak onto a pooled connection"


@pytest.mark.asyncio
async def test_offset_walks_ranked_results_without_overlap():
    import uuid

    scores = [0.9 - i * 0.01 for i in range(20)]
    uid = uuid.uuid4()
    page1 = await corpus_search.search_chunks(
        _FakeSession(scores), uid, [0.1] * 4, threshold=0.5, limit=5)
    page2 = await corpus_search.search_chunks(
        _FakeSession(scores), uid, [0.1] * 4, threshold=0.5, limit=5, offset=5)
    assert [h.chunk_id for h in page1] == [0, 1, 2, 3, 4]
    assert [h.chunk_id for h in page2] == [5, 6, 7, 8, 9]
    assert not ({h.chunk_id for h in page1} & {h.chunk_id for h in page2})


@pytest.mark.asyncio
async def test_total_counts_the_corpus_not_the_page():
    """`total` is what makes a short page distinguishable from a complete one."""
    import uuid

    scores = [0.9] * 40 + [0.1] * 10
    total = await corpus_search.count_matching_chunks(
        _FakeSession(scores), uuid.uuid4(), [0.1] * 4, threshold=0.5)
    assert total == 40


@pytest.mark.asyncio
async def test_offset_is_included_in_the_fetch_budget():
    """Page 2 must not be starved by a fetch sized for page 1 alone."""
    import uuid

    session = _FakeSession([0.9] * 200)
    await corpus_search.search_chunks(
        session, uuid.uuid4(), [0.1] * 4, threshold=0.5, limit=10, offset=50)
    # limit+offset drives the budget, not limit.
    assert any("LIMIT :fetch" in s for s in session.statements)


@pytest.mark.asyncio
async def test_open_ended_ranges_are_allowed_in_either_direction():
    """"Anything since March" is a real request.

    Requiring both bounds would force callers to invent a far-future date, so
    each bound stands alone. Verified against production: an open `from` and
    an open `to` at the same cut sum exactly to the unfiltered total
    (279 + 719 = 998) — no passage lost, none counted twice.
    """
    from datetime import datetime, timezone

    from vts.services.corpus_search import _scope_clauses

    cut = datetime(2026, 7, 1, tzinfo=timezone.utc)
    only_from, p1 = _scope_clauses(
        recording_id=None, task_ids=None, created_from=cut, created_to=None)
    only_to, p2 = _scope_clauses(
        recording_id=None, task_ids=None, created_from=None, created_to=cut)
    assert "created_from" in p1 and "created_to" not in p1
    assert "created_to" in p2 and "created_from" not in p2
    assert ">=" in only_from and "<=" in only_to


def test_dates_filter_on_the_recording_not_the_chunk():
    """A chunk's date is when the corpus was indexed, not when people spoke.

    On production 6624 of 6694 chunks carry a different day from their
    recording — the corpus was indexed in one afternoon. Filtering on the
    chunk would answer a different question while looking correct.
    """
    from datetime import datetime, timezone

    from vts.services.corpus_search import _scope_clauses

    sql, _ = _scope_clauses(
        recording_id=None, task_ids=None,
        created_from=datetime(2026, 1, 1, tzinfo=timezone.utc), created_to=None)
    assert "r.created_at" in sql, "must filter on the recording's date"
    assert "c.created_at" not in sql, "must NOT filter on the chunk's date"


def test_no_filters_produce_no_clauses():
    from vts.services.corpus_search import _scope_clauses

    sql, params = _scope_clauses(
        recording_id=None, task_ids=None, created_from=None, created_to=None)
    assert sql == "" and params == {}
