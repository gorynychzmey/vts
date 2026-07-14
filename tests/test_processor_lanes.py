import asyncio
import uuid

import pytest

from vts.db.models import TaskStatus
from vts.pipeline.processor import TaskProcessor
from vts.worker.lanes import LaneManager


class _SettingsStub:
    lane_network_slots = 1
    lane_ffmpeg_slots = 1
    lane_gpu_slots = 1
    gpu_asr_burst = 3
    night_mode_enabled = False
    night_mode_start_hour = 22
    night_mode_end_hour = 7


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)


class _StubSession:
    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def commit(self) -> None:
        return None


class _StubRepo:
    """Repo whose conditional transition always reports a real change."""

    def __init__(self, session: object) -> None:
        self.session = session

    async def transition_task_status(self, task_id, from_statuses, to_status) -> bool:
        return True


def _make_processor(lanes: LaneManager, bus: _CapturingBus, monkeypatch) -> TaskProcessor:
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.lanes = lanes
    proc.bus = bus
    proc.session_factory = lambda: _StubSession()
    monkeypatch.setattr("vts.pipeline.processor.Repo", _StubRepo)
    return proc


@pytest.mark.asyncio
async def test_gpu_slot_emits_waiting_then_running_when_contended(monkeypatch) -> None:
    lanes = LaneManager(_SettingsStub())
    bus = _CapturingBus()
    proc = _make_processor(lanes, bus, monkeypatch)

    holder_id = uuid.uuid4()
    task_id = uuid.uuid4()

    holder_release = asyncio.Event()
    holder_acquired = asyncio.Event()

    async def _holder() -> None:
        async with lanes.slot("gpu", holder_id, "asr"):
            holder_acquired.set()
            await holder_release.wait()

    holder_task = asyncio.create_task(_holder())
    await holder_acquired.wait()

    # The single gpu slot is taken -> our task must enqueue and go waiting.
    contender_entered = asyncio.Event()

    async def _contender() -> None:
        async with proc._gpu_slot(task_id, "user-1", "llm"):
            contender_entered.set()

    contender_task = asyncio.create_task(_contender())

    # Give the contender a chance to enqueue and fire on_wait.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    waiting_events = [e for e in bus.events if e["data"].get("status") == TaskStatus.waiting.value]
    assert waiting_events, "expected a waiting event while the slot was held"
    assert waiting_events[0]["data"]["queue"] == "gpu"
    assert waiting_events[0]["user_id"] == "user-1"
    assert waiting_events[0]["task_id"] == str(task_id)
    assert not contender_entered.is_set()

    # Release the holder -> contender is granted -> on_grant flips to running.
    holder_release.set()
    await asyncio.wait_for(contender_task, timeout=1.0)
    await asyncio.wait_for(holder_task, timeout=1.0)

    assert contender_entered.is_set()
    running_events = [e for e in bus.events if e["data"].get("status") == TaskStatus.running.value]
    assert running_events, "expected a running event after the slot was granted"
    # waiting must precede running in emission order.
    assert bus.events.index(waiting_events[0]) < bus.events.index(running_events[0])


@pytest.mark.asyncio
async def test_gpu_slot_immediate_grant_emits_no_transition(monkeypatch) -> None:
    lanes = LaneManager(_SettingsStub())
    bus = _CapturingBus()
    proc = _make_processor(lanes, bus, monkeypatch)

    task_id = uuid.uuid4()
    async with proc._gpu_slot(task_id, "user-1", "asr"):
        pass

    # No contention -> neither on_wait nor on_grant fires -> no status events.
    assert bus.events == []
