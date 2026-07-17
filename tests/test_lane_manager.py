import asyncio
import uuid
from pathlib import Path

import pytest
from types import SimpleNamespace

from vts.worker.lanes import LaneManager


def _settings(**over):
    base = dict(worker_max_active_tasks=4, lane_network_slots=1, lane_ffmpeg_slots=2,
                lane_gpu_slots=1, lane_diarize_slots=2, gpu_asr_burst=3,
                night_mode_enabled=False, night_mode_start_hour=22, night_mode_end_hour=7)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_immediate_grant_skips_callbacks():
    mgr = LaneManager(_settings())
    called = []
    async def on_wait(): called.append("wait")
    async with mgr.slot("network", uuid.uuid4(), on_wait=on_wait):
        pass
    assert called == []


@pytest.mark.asyncio
async def test_fifo_within_lane():
    mgr = LaneManager(_settings())
    order = []
    async def hold(tid, delay):
        async with mgr.slot("network", tid):
            order.append(tid)
            await asyncio.sleep(delay)
    t1, t2, t3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tasks = [asyncio.create_task(hold(t, 0.01)) for t in (t1, t2, t3)]
    await asyncio.gather(*tasks)
    assert order == [t1, t2, t3]


@pytest.mark.asyncio
async def test_gpu_asr_beats_llm():
    mgr = LaneManager(_settings())
    order = []
    async def use(cls, tag):
        async with mgr.slot("gpu", uuid.uuid4(), cls):
            order.append(tag)
            await asyncio.sleep(0.01)
    first = asyncio.create_task(use("llm", "llm-holder"))
    await asyncio.sleep(0.001)  # holder occupies the slot
    q_llm = asyncio.create_task(use("llm", "llm-2"))
    await asyncio.sleep(0.001)
    q_asr = asyncio.create_task(use("asr", "asr-1"))  # enqueued after llm-2, must be granted first
    await asyncio.gather(first, q_llm, q_asr)
    assert order == ["llm-holder", "asr-1", "llm-2"]


@pytest.mark.asyncio
async def test_asr_burst_yields_to_llm():
    mgr = LaneManager(_settings(gpu_asr_burst=2))
    order = []
    async def use(cls, tag):
        async with mgr.slot("gpu", uuid.uuid4(), cls):
            order.append(tag)
            await asyncio.sleep(0.005)
    holder = asyncio.create_task(use("llm", "h"))
    await asyncio.sleep(0.001)
    llm_w = asyncio.create_task(use("llm", "llm-w"))
    asr = [asyncio.create_task(use("asr", f"a{i}")) for i in range(3)]
    await asyncio.gather(holder, llm_w, *asr)
    # 2 asr grants, then forced llm, then remaining asr
    assert order == ["h", "a0", "a1", "llm-w", "a2"]


@pytest.mark.asyncio
async def test_cancel_removes_waiter():
    mgr = LaneManager(_settings())
    tid_holder, tid_wait = uuid.uuid4(), uuid.uuid4()
    entered = asyncio.Event()
    async def holder():
        async with mgr.slot("network", tid_holder):
            entered.set()
            await asyncio.sleep(0.05)
    h = asyncio.create_task(holder())
    await entered.wait()
    async def waiter():
        async with mgr.slot("network", tid_wait):
            pass
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.001)
    assert mgr.snapshot()["network"] == [str(tid_wait)]
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert mgr.snapshot()["network"] == []
    await h


@pytest.mark.asyncio
async def test_night_mode_blocks_gpu_and_retries():
    allowed = {"v": False}
    mgr = LaneManager(_settings(), night_allowed=lambda: allowed["v"])
    got = asyncio.Event()
    async def use():
        async with mgr.slot("gpu", uuid.uuid4(), "llm"):
            got.set()
    t = asyncio.create_task(use())
    await asyncio.sleep(0.01)
    assert not got.is_set()
    allowed["v"] = True
    mgr.poke()  # test hook: force re-evaluation instead of waiting 30s
    await asyncio.wait_for(got.wait(), 1)
    await t


@pytest.mark.asyncio
async def test_exception_in_body_releases_slot():
    mgr = LaneManager(_settings(lane_network_slots=1))
    with pytest.raises(RuntimeError):
        async with mgr.slot("network", uuid.uuid4()):
            raise RuntimeError("boom")
    # slot must have been released despite the exception: a second acquire
    # succeeds immediately rather than blocking forever.
    async def acquire():
        async with mgr.slot("network", uuid.uuid4()):
            pass
    await asyncio.wait_for(acquire(), 1)


@pytest.mark.asyncio
async def test_streak_resets_after_forced_llm_grant():
    # Two burst cycles with an intervening forced llm grant. With burst=2,
    # after the forced llm resets the streak, a fresh set of asr grants must
    # again get a full burst of 2 before the SECOND forced llm. A stale
    # (non-reset) streak of >=2 would force the second llm too early and
    # change the grant order.
    mgr = LaneManager(_settings(gpu_asr_burst=2))
    order = []
    async def use(cls, tag, delay=0.005):
        async with mgr.slot("gpu", uuid.uuid4(), cls):
            order.append(tag)
            await asyncio.sleep(delay)
    # holder occupies the single gpu slot while we stage the queues.
    holder = asyncio.create_task(use("llm", "h"))
    await asyncio.sleep(0.001)
    # Two llm waiters and four asr waiters, asr enqueued after the first llm.
    llm1 = asyncio.create_task(use("llm", "llm1"))
    await asyncio.sleep(0.001)
    asr = []
    for i in range(4):
        asr.append(asyncio.create_task(use("asr", f"a{i}")))
        await asyncio.sleep(0.001)
    llm2 = asyncio.create_task(use("llm", "llm2"))
    await asyncio.gather(holder, llm1, llm2, *asr)
    # h releases -> a0, a1 (streak hits burst=2) -> llm1 forced (streak reset
    # to 0) -> a2, a3 (streak hits 2 again) -> llm2 forced.
    # If the reset were removed, streak would stay >=2 and llm2 would be
    # forced right after a2, yielding [..., "a2", "llm2", "a3"].
    assert order == ["h", "a0", "a1", "llm1", "a2", "a3", "llm2"]


@pytest.mark.asyncio
async def test_on_change_snapshots():
    snaps = []
    async def on_change(s): snaps.append(s)
    mgr = LaneManager(_settings(), on_change=on_change)
    async with mgr.slot("ffmpeg", uuid.uuid4()):
        pass
    assert snaps  # at least grant + release
    assert set(snaps[-1].keys()) == {"network", "ffmpeg", "gpu_asr", "gpu_llm", "diarize"}


@pytest.mark.asyncio
async def test_on_change_failure_does_not_wedge_gpu_lane():
    # Regression: a raising on_change must not leak the gpu slot. gpu has a
    # single slot, so the very first acquire takes the immediate-grant path
    # in _SlotContext.__aenter__, where _active[lane] is incremented BEFORE
    # _notify() is awaited. If the exception from on_change escaped _notify,
    # it would propagate out of __aenter__, the `async with` body would never
    # run, __aexit__ would never run (so the slot is never released), and the
    # gpu lane would be wedged until worker restart.
    async def on_change(snapshot):
        raise RuntimeError("redis blip")

    mgr = LaneManager(_settings(), on_change=on_change)

    entered = False
    exc_escaped = None
    try:
        async with mgr.slot("gpu", uuid.uuid4(), "llm"):
            entered = True
    except Exception as exc:  # pragma: no cover - only hit if the bug regresses
        exc_escaped = exc

    assert exc_escaped is None  # (b) no exception escapes to the caller
    assert entered  # the immediate-grant path did run the body

    # (a) slot was released, not leaked: a second acquire on the same
    # 1-slot gpu lane must succeed promptly rather than hang forever.
    async def acquire_again():
        async with mgr.slot("gpu", uuid.uuid4(), "asr"):
            pass

    await asyncio.wait_for(acquire_again(), 1)


@pytest.mark.asyncio
async def test_diarize_lane_caps_concurrency():
    """Two diarizations run side by side; the third waits for a slot.

    Without this cap the worker's four in-flight tasks could start four
    pyannote runs at once, each wanting 8 threads on a 16-core box — all four
    then finish slower than if two had simply queued.
    """
    mgr = LaneManager(_settings(lane_diarize_slots=2))
    peak = 0
    active = 0

    async def hold(tid):
        nonlocal peak, active
        async with mgr.slot("diarize", tid):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*[hold(uuid.uuid4()) for _ in range(4)])
    assert peak == 2


@pytest.mark.asyncio
async def test_diarize_lane_is_independent_of_gpu():
    """Diarization must not queue behind Whisper: it runs on the CPU.

    The whole point of vts-bv7 is that the two use different compute, so a
    busy gpu lane must never hold up a diarization.
    """
    mgr = LaneManager(_settings(lane_gpu_slots=1, lane_diarize_slots=1))
    async with mgr.slot("gpu", uuid.uuid4(), "asr"):
        # gpu is now fully occupied; diarize must still be grantable at once.
        async def take_diarize():
            async with mgr.slot("diarize", uuid.uuid4()):
                return "granted"

        assert await asyncio.wait_for(take_diarize(), 1) == "granted"


@pytest.mark.asyncio
async def test_diarize_waiters_appear_in_snapshot():
    """A queued diarization must be visible to the UI's queue position.

    _get_lane_positions maps this key to the public "diarize" queue; if the
    snapshot omitted waiters, a user would sit in an invisible queue.
    """
    mgr = LaneManager(_settings(lane_diarize_slots=1))
    waiting = uuid.uuid4()
    async with mgr.slot("diarize", uuid.uuid4()):
        task = asyncio.create_task(_hold_briefly(mgr, waiting))
        await asyncio.sleep(0.01)
        assert mgr.snapshot()["diarize"] == [str(waiting)]
    await task


async def _hold_briefly(mgr, tid):
    async with mgr.slot("diarize", tid):
        await asyncio.sleep(0)


def test_diarize_slots_derived_from_cores(monkeypatch):
    """0 means "size it to the host" — this is a public project."""
    from vts.worker import lanes

    monkeypatch.setattr(lanes, "_available_cores", lambda: 16)
    assert lanes._diarize_slots(_settings(lane_diarize_slots=0)) == 2
    monkeypatch.setattr(lanes, "_available_cores", lambda: 4)
    assert lanes._diarize_slots(_settings(lane_diarize_slots=0)) == 1  # never zero
    monkeypatch.setattr(lanes, "_available_cores", lambda: 1)
    assert lanes._diarize_slots(_settings(lane_diarize_slots=0)) == 1
    # an explicit value always wins over the derivation
    assert lanes._diarize_slots(_settings(lane_diarize_slots=3)) == 3


def test_available_cores_honours_container_quota(monkeypatch, tmp_path):
    """A --cpus 2 container still sees 16 in os.cpu_count().

    Sizing from the host count there would hand out slots for cores the process
    cannot use: the quota throttles it instead of hiding the CPUs.
    """
    from vts.worker import lanes

    quota = tmp_path / "cpu.max"
    quota.write_text("200000 100000")  # 2 cores' worth
    monkeypatch.setattr(lanes, "Path", lambda p: quota if p == "/sys/fs/cgroup/cpu.max" else Path(p))
    monkeypatch.setattr(lanes.os, "cpu_count", lambda: 16)
    assert lanes._available_cores() == 2

    quota.write_text("max 100000")  # unlimited: fall through to affinity/host
    monkeypatch.setattr(lanes.os, "sched_getaffinity", lambda _pid: set(range(16)))
    assert lanes._available_cores() == 16
