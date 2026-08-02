"""The worker must exit on SIGTERM instead of ignoring it (vts-9er).

Measured before this fix: `podman stop -t 30 vts-worker` waited the full 30s
and then SIGKILLed, because the process is PID 1 with no handler installed.
"""
from __future__ import annotations

import asyncio
import signal

import pytest

import vts.worker.main as worker_main


@pytest.mark.asyncio
async def test_run_worker_cancels_the_loop_on_sigterm(monkeypatch):
    """SIGTERM must cancel worker_loop and let _run_worker return normally."""
    started = asyncio.Event()
    cleaned_up = False

    async def fake_worker_loop() -> None:
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            # Stands in for the real loop's teardown block.
            cleaned_up = True

    monkeypatch.setattr(worker_main, "worker_loop", fake_worker_loop)

    runner = asyncio.create_task(worker_main._run_worker())
    await asyncio.wait_for(started.wait(), timeout=5)

    signal.raise_signal(signal.SIGTERM)

    # Must finish promptly — the point of the fix is not waiting for a timeout.
    await asyncio.wait_for(runner, timeout=5)
    assert cleaned_up, "worker_loop's finally block must run"


@pytest.mark.asyncio
async def test_run_worker_returns_normally_without_a_signal(monkeypatch):
    """A loop that ends on its own must not be reported as a failure."""

    async def fake_worker_loop() -> None:
        return

    monkeypatch.setattr(worker_main, "worker_loop", fake_worker_loop)
    await asyncio.wait_for(worker_main._run_worker(), timeout=5)


@pytest.mark.asyncio
async def test_run_worker_removes_its_handlers(monkeypatch):
    """The handlers must not outlive the run.

    pytest runs many tests in one process; a handler left installed on the
    loop would fire during an unrelated later test. Production only ever runs
    this once, so this guards the test suite rather than the container.
    """
    async def fake_worker_loop() -> None:
        return

    monkeypatch.setattr(worker_main, "worker_loop", fake_worker_loop)
    await asyncio.wait_for(worker_main._run_worker(), timeout=5)

    loop = asyncio.get_running_loop()
    # remove_signal_handler returns False when nothing was installed.
    assert loop.remove_signal_handler(signal.SIGTERM) is False
    assert loop.remove_signal_handler(signal.SIGINT) is False
