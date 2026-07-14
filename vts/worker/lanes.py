from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

_LANES = ("network", "ffmpeg", "gpu")

_log = logging.getLogger("vts.worker")


def _default_night_allowed(settings: Any) -> Callable[[], bool]:
    def allowed() -> bool:
        if not settings.night_mode_enabled:
            return True
        now_hour = datetime.now().hour
        start = settings.night_mode_start_hour
        end = settings.night_mode_end_hour
        return (start <= now_hour) or (now_hour < end) if start > end else start <= now_hour < end
    return allowed


class _Waiter:
    __slots__ = ("task_id", "future")

    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()


class LaneManager:
    """In-process slot scheduler for pipeline resource lanes.

    gpu lane has two FIFO classes: asr (priority) and llm, with an
    anti-starvation burst limit; night mode gates gpu grants only.
    """

    def __init__(
        self,
        settings: Any,
        *,
        night_allowed: Callable[[], bool] | None = None,
        on_change: Callable[[dict[str, list[str]]], Awaitable[None]] | None = None,
    ) -> None:
        self._slots = {
            "network": max(settings.lane_network_slots, 1),
            "ffmpeg": max(settings.lane_ffmpeg_slots, 1),
            "gpu": max(settings.lane_gpu_slots, 1),
        }
        self._active = {name: 0 for name in _LANES}
        self._queues: dict[tuple[str, str], deque[_Waiter]] = {
            ("network", "main"): deque(),
            ("ffmpeg", "main"): deque(),
            ("gpu", "asr"): deque(),
            ("gpu", "llm"): deque(),
        }
        self._asr_streak = 0
        self._burst = max(settings.gpu_asr_burst, 1)
        self._night_allowed = night_allowed or _default_night_allowed(settings)
        self._on_change = on_change
        self._night_timer: asyncio.TimerHandle | None = None

    # -- public -----------------------------------------------------------

    def slot(
        self,
        lane: str,
        task_id: uuid.UUID,
        cls: str = "main",
        *,
        on_wait: Callable[[], Awaitable[None]] | None = None,
        on_grant: Callable[[], Awaitable[None]] | None = None,
    ) -> "_SlotContext":
        if lane not in _LANES:
            raise ValueError(f"unknown lane: {lane}")
        if lane != "gpu":
            cls = "main"
        elif cls not in ("asr", "llm"):
            raise ValueError(f"unknown gpu class: {cls}")
        return _SlotContext(self, lane, cls, task_id, on_wait, on_grant)

    def snapshot(self) -> dict[str, list[str]]:
        return {
            "network": [str(w.task_id) for w in self._queues[("network", "main")]],
            "ffmpeg": [str(w.task_id) for w in self._queues[("ffmpeg", "main")]],
            "gpu_asr": [str(w.task_id) for w in self._queues[("gpu", "asr")]],
            "gpu_llm": [str(w.task_id) for w in self._queues[("gpu", "llm")]],
        }

    def poke(self) -> None:
        """Re-evaluate pending grants (used by the night-mode retry timer and tests)."""
        for lane in _LANES:
            self._grant_pending(lane)

    # -- internals ---------------------------------------------------------

    async def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change(self.snapshot())
        except Exception:
            # The snapshot publish is a best-effort cache (10s TTL); its
            # failure must never undo slot bookkeeping already committed by
            # the caller, nor propagate out of __aenter__/__aexit__ (which
            # would leak a slot forever, e.g. the 1-slot gpu lane).
            _log.warning("lane snapshot on_change callback failed", exc_info=True)

    def _has_waiters(self, lane: str) -> bool:
        if lane == "gpu":
            return bool(self._queues[("gpu", "asr")] or self._queues[("gpu", "llm")])
        return bool(self._queues[(lane, "main")])

    def _try_immediate(self, lane: str) -> bool:
        if self._active[lane] >= self._slots[lane]:
            return False
        if self._has_waiters(lane):
            return False  # fairness: join the queue behind existing waiters
        if lane == "gpu" and not self._night_allowed():
            self._schedule_night_retry()
            return False
        self._active[lane] += 1
        return True

    def _pick_gpu_queue(self) -> deque[_Waiter] | None:
        asr_q = self._queues[("gpu", "asr")]
        llm_q = self._queues[("gpu", "llm")]
        if asr_q and llm_q and self._asr_streak >= self._burst:
            return llm_q
        if asr_q:
            return asr_q
        if llm_q:
            return llm_q
        return None

    def _grant_pending(self, lane: str) -> None:
        while self._active[lane] < self._slots[lane]:
            if lane == "gpu":
                if not self._night_allowed():
                    self._schedule_night_retry()
                    return
                queue = self._pick_gpu_queue()
                if queue is None:
                    return
                waiter = queue.popleft()
                if queue is self._queues[("gpu", "asr")]:
                    self._asr_streak += 1
                else:
                    self._asr_streak = 0
            else:
                queue = self._queues[(lane, "main")]
                if not queue:
                    return
                waiter = queue.popleft()
            self._active[lane] += 1
            if not waiter.future.done():
                waiter.future.set_result(None)

    def _schedule_night_retry(self) -> None:
        if self._night_timer is not None:
            return
        loop = asyncio.get_running_loop()

        def _retry() -> None:
            self._night_timer = None
            if self._has_waiters("gpu"):
                self._grant_pending("gpu")
                if self._has_waiters("gpu"):
                    self._schedule_night_retry()

        self._night_timer = loop.call_later(30, _retry)

    def _release(self, lane: str) -> None:
        self._active[lane] = max(self._active[lane] - 1, 0)
        self._grant_pending(lane)


class _SlotContext:
    def __init__(
        self,
        mgr: LaneManager,
        lane: str,
        cls: str,
        task_id: uuid.UUID,
        on_wait: Callable[[], Awaitable[None]] | None,
        on_grant: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._mgr, self._lane, self._cls = mgr, lane, cls
        self._task_id, self._on_wait, self._on_grant = task_id, on_wait, on_grant

    async def __aenter__(self) -> "_SlotContext":
        mgr = self._mgr
        if mgr._try_immediate(self._lane):
            await mgr._notify()
            return self
        waiter = _Waiter(self._task_id)
        mgr._queues[(self._lane, self._cls)].append(waiter)
        await mgr._notify()
        if self._on_wait is not None:
            await self._on_wait()
        try:
            await waiter.future
        except asyncio.CancelledError:
            try:
                mgr._queues[(self._lane, self._cls)].remove(waiter)
            except ValueError:
                # already granted between cancel and cleanup: release the slot
                mgr._release(self._lane)
            await mgr._notify()
            raise
        await mgr._notify()
        if self._on_grant is not None:
            await self._on_grant()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._mgr._release(self._lane)
        await self._mgr._notify()
