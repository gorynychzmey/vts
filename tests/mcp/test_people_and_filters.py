"""Voice filters and the people registry on task listings.

These cover what a client actually reasons with: who appears in a recording,
and whether a filter that finds nobody says so instead of quietly returning
everything.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from tests.mcp.conftest import FakeRepo, FakeTask, FakeUser
from vts.mcp.tools import list_tasks


@dataclass
class _Person:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str


def _seed_with_people() -> tuple[FakeRepo, FakeUser, list[uuid.UUID]]:
    """Three tasks: two with identified voices, one without."""
    repo = FakeRepo()
    user = FakeUser(id=str(uuid.uuid4()))
    user_id = uuid.UUID(user.id)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids: list[uuid.UUID] = []
    for i in range(3):
        t = FakeTask(
            id=uuid.uuid4(), user_id=user_id, source_url=f"https://x/{i}",
            source_title=f"title-{i}", status="completed",
            created_at=base + timedelta(minutes=i),
            updated_at=base + timedelta(minutes=i),
        )
        repo.tasks[t.id] = t
        ids.append(t.id)

    diana = _Person(uuid.uuid4(), user_id, "Диана")
    yana = _Person(uuid.uuid4(), user_id, "Яна")
    repo.speakers = {diana.id: diana, yana.id: yana}
    repo.task_people = {ids[0]: ["Диана", "Яна"], ids[1]: ["Яна"]}
    return repo, user, ids


@pytest.mark.asyncio
async def test_people_are_names_not_speaker_labels():
    repo, user, ids = _seed_with_people()
    page = await list_tasks(user=user, repo=repo, limit=10)
    by_id = {t.task_id: t for t in page.tasks}
    assert by_id[ids[0]].people == ["Диана", "Яна"]
    # Never diarised: empty, and no SPEAKER_NN tag leaking through.
    assert by_id[ids[2]].people == []


@pytest.mark.asyncio
async def test_person_filter_selects_only_their_tasks():
    repo, user, ids = _seed_with_people()
    page = await list_tasks(user=user, repo=repo, limit=10, person="Диана")
    assert [t.task_id for t in page.tasks] == [ids[0]]


@pytest.mark.asyncio
async def test_person_filter_matches_part_of_a_name_case_insensitively():
    """Callers type what a human would say, not an exact registry entry."""
    repo, user, ids = _seed_with_people()
    page = await list_tasks(user=user, repo=repo, limit=10, person="ян")
    assert {t.task_id for t in page.tasks} == {ids[0], ids[1]}


@pytest.mark.asyncio
async def test_unknown_person_returns_empty_not_everything():
    """The dangerous failure mode: a filter that silently stops filtering.

    Returning the unfiltered list would read to a client as "these are the
    tasks that person appears in" — a confident wrong answer, worse than none.
    """
    repo, user, _ids = _seed_with_people()
    page = await list_tasks(user=user, repo=repo, limit=10, person="Нет Такого")
    assert page.tasks == []
    assert page.has_more is False
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_diarized_filter_both_directions():
    repo, user, ids = _seed_with_people()
    yes = await list_tasks(user=user, repo=repo, limit=10, diarized=True)
    assert {t.task_id for t in yes.tasks} == {ids[0], ids[1]}
    no = await list_tasks(user=user, repo=repo, limit=10, diarized=False)
    assert [t.task_id for t in no.tasks] == [ids[2]]


@pytest.mark.asyncio
async def test_people_lookup_is_one_query_for_the_whole_page():
    """A page of N tasks must not cost N queries."""
    repo, user, _ids = _seed_with_people()
    calls: list[int] = []
    original = repo.speaker_names_for_tasks

    async def counting(user_id, task_ids):
        calls.append(len(task_ids))
        return await original(user_id, task_ids)

    repo.speaker_names_for_tasks = counting
    await list_tasks(user=user, repo=repo, limit=10)
    assert calls == [3], f"expected one batched call for 3 tasks, got {calls}"


def test_speaker_labels_map_by_label_not_by_position():
    """A quote must carry the name of whoever actually said it.

    Real data (production, 2026-08-31): one passage had labels
    ['SPEAKER_03', 'SPEAKER_04', 'SPEAKER_05'] where 04 and 05 are the SAME
    person. Pairing names onto sorted labels by position would have invented a
    third speaker and misattributed the quote.
    """
    from vts.mcp.tools_registry.search import _named_speakers

    class _Hit:
        source_task_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        speakers = ["SPEAKER_03", "SPEAKER_04", "SPEAKER_05"]

    mapping = {
        _Hit.source_task_id: {
            "SPEAKER_03": "Диана",
            "SPEAKER_04": "Яна",
            "SPEAKER_05": "Яна",
        }
    }
    assert _named_speakers(_Hit(), mapping) == ["Диана", "Яна", "Яна"]


def test_unresolved_label_is_kept_not_dropped():
    """Dropping it would make a two-person passage look like a monologue."""
    from vts.mcp.tools_registry.search import _named_speakers

    class _Hit:
        source_task_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        speakers = ["SPEAKER_01", "SPEAKER_09"]

    mapping = {_Hit.source_task_id: {"SPEAKER_01": "Диана"}}
    assert _named_speakers(_Hit(), mapping) == ["Диана", "SPEAKER_09"]


def test_orphaned_recording_keeps_raw_labels():
    """No task, no names — and that must not raise."""
    from vts.mcp.tools_registry.search import _named_speakers

    class _Hit:
        source_task_id = None
        speakers = ["SPEAKER_00"]

    assert _named_speakers(_Hit(), {}) == ["SPEAKER_00"]
