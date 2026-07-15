import pytest
from vts.db.models import TaskStatus
from vts.services import task_status as ts

ALL = list(TaskStatus)

@pytest.mark.parametrize("status,expected", [
    (TaskStatus.queued, False), (TaskStatus.running, True), (TaskStatus.waiting, True),
    (TaskStatus.paused, False), (TaskStatus.completed, False), (TaskStatus.archived, False),
    (TaskStatus.failed, False), (TaskStatus.canceled, False),
])
def test_is_active(status, expected):
    assert ts.is_active(status) is expected

@pytest.mark.parametrize("status,expected", [
    (TaskStatus.queued, True), (TaskStatus.running, False), (TaskStatus.waiting, True),
    (TaskStatus.paused, False), (TaskStatus.completed, False), (TaskStatus.archived, False),
    (TaskStatus.failed, False), (TaskStatus.canceled, False),
])
def test_is_pending(status, expected):
    assert ts.is_pending(status) is expected

def test_can_pause_matches_legacy_set():
    assert {s for s in ALL if ts.can_pause(s)} == {TaskStatus.queued, TaskStatus.running, TaskStatus.waiting}

def test_can_resume_matches_legacy_set():
    assert {s for s in ALL if ts.can_resume(s)} == {TaskStatus.paused, TaskStatus.failed}

def test_can_archive_matches_legacy_set():
    assert {s for s in ALL if ts.can_archive(s)} == {TaskStatus.completed, TaskStatus.failed}

def test_shows_progress_set():
    assert {s for s in ALL if ts.shows_progress(s)} == {
        TaskStatus.running, TaskStatus.waiting, TaskStatus.completed, TaskStatus.failed}

def test_is_finished_set():
    assert {s for s in ALL if ts.is_finished(s)} == {
        TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled, TaskStatus.archived}

def test_skippable_on_start_set():
    assert {s for s in ALL if ts.is_skippable_on_start(s)} == {
        TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}

def test_terminal_for_wait_set():
    assert {s for s in ALL if ts.is_terminal_for_wait(s)} == {
        TaskStatus.completed, TaskStatus.failed, TaskStatus.canceled}

def test_status_flags_covers_all_statuses_and_matches_predicates():
    flags = ts.status_flags()
    assert set(flags) == {s.value for s in ALL}
    for s in ALL:
        f = flags[s.value]
        assert f == {
            "is_active": ts.is_active(s), "is_pending": ts.is_pending(s),
            "is_finished": ts.is_finished(s), "shows_progress": ts.shows_progress(s),
            "can_pause": ts.can_pause(s), "can_resume": ts.can_resume(s),
            "can_archive": ts.can_archive(s),
        }
