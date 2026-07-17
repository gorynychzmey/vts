"""reconcile_diarization_jobs: startup cleanup of orphaned sidecar jobs (vts-8y6)."""
import logging
from types import SimpleNamespace


from vts.worker.main import reconcile_diarization_jobs


class _FakeDiarization:
    def __init__(self, jobs, list_raises=False, cancel_raises=False):
        self._jobs = jobs
        self._list_raises = list_raises
        self._cancel_raises = cancel_raises
        self.cancelled: list[str] = []

    async def list_jobs(self):
        if self._list_raises:
            raise RuntimeError("sidecar down")
        return list(self._jobs)

    async def cancel(self, job_id):
        if self._cancel_raises:
            raise RuntimeError("cancel failed")
        self.cancelled.append(job_id)


def _processor(diar):
    return SimpleNamespace(diarization=diar)


async def test_cancels_every_listed_job():
    # After requeue, no task owns a running job, so every listed job is stale.
    diar = _FakeDiarization(["job-a", "job-b", "job-c"])
    await reconcile_diarization_jobs(_processor(diar), logging.getLogger("test"))
    assert set(diar.cancelled) == {"job-a", "job-b", "job-c"}


async def test_no_jobs_cancels_nothing():
    diar = _FakeDiarization([])
    await reconcile_diarization_jobs(_processor(diar), logging.getLogger("test"))
    assert diar.cancelled == []


async def test_list_failure_does_not_break_startup():
    # Reconciliation is an optimisation over the TTL; a sidecar that errors on
    # /jobs must not stop the worker from booting.
    diar = _FakeDiarization([], list_raises=True)
    await reconcile_diarization_jobs(_processor(diar), logging.getLogger("test"))
    assert diar.cancelled == []


async def test_cancel_failure_does_not_break_startup():
    # Likewise if a cancel raises: reconcile swallows it rather than aborting.
    diar = _FakeDiarization(["job-a"], cancel_raises=True)
    await reconcile_diarization_jobs(_processor(diar), logging.getLogger("test"))
    # It swallowed the raise; nothing was recorded as cancelled.
    assert diar.cancelled == []
