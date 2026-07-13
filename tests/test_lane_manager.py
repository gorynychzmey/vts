import asyncio
import uuid

import pytest
from types import SimpleNamespace

from vts.worker.lanes import LaneManager


def _settings(**over):
    base = dict(worker_max_active_tasks=4, lane_network_slots=1, lane_ffmpeg_slots=2,
                lane_gpu_slots=1, gpu_asr_burst=3,
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
async def test_on_change_snapshots():
    snaps = []
    async def on_change(s): snaps.append(s)
    mgr = LaneManager(_settings(), on_change=on_change)
    async with mgr.slot("ffmpeg", uuid.uuid4()):
        pass
    assert snaps  # at least grant + release
    assert set(snaps[-1].keys()) == {"network", "ffmpeg", "gpu_asr", "gpu_llm"}
