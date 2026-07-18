"""Regression test for the match_speakers pause/resume lifecycle bug (vts-80i).

Bug: TaskAwaitingInput is raised FROM INSIDE a step's run() (unlike TaskPaused,
which is raised BETWEEN steps in check_paused). Because it propagates through
_run_step's `except Exception` handler, the step used to be marked `failed`.
On resume, `already_done` is gated on `step.status == StepStatus.completed`,
so a `failed` match_speakers step never short-circuits: run() executes again,
recomputes matches, and if any speaker is still unresolved (by design — the
user chose to leave them anonymous), decide_pause fires again and the task
re-enters awaiting_input forever.

Fix: _run_step must catch TaskAwaitingInput ahead of the generic Exception
handler, mark the step `completed` (it DID do its job: wrote
speaker_matches.json and made a valid pause decision), then re-raise so the
outer handler still sets the task to awaiting_input.

This test exercises TaskProcessor._run_step directly (same harness pattern as
tests/test_processor_lanes.py: TaskProcessor.__new__ + stub Repo/session),
dispatching through the real STEP_REGISTRY to a fake step that raises
TaskAwaitingInput, and asserts:
  1. The step status lands on `completed`, not `failed`.
  2. TaskAwaitingInput still propagates out of _run_step (so the outer
     pipeline handler in process_task can catch it and set the task status).

A second test (against the real MatchSpeakersStep.already_done, already
covered in tests/test_speaker_match_step.py but re-asserted here for the
resume story) confirms that once the step is `completed` and
speaker_matches.json exists on disk, already_done returns True — i.e. a
resumed task's DAG loop will skip straight past match_speakers instead of
re-running it and re-pausing.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path

import pytest

from vts.db.models import StepStatus
from vts.pipeline.context import PipelineContext
from vts.pipeline.processor import TaskAwaitingInput, TaskProcessor
from vts.pipeline.steps.base import Step
from vts.pipeline.steps.registry import STEP_REGISTRY


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


class _StubStep:
    def __init__(self) -> None:
        self.status = StepStatus.pending
        self.message: str | None = None


class _RunStepRepo:
    """Minimal Repo stand-in exposing exactly the surface _run_step touches."""

    def __init__(self, session: object) -> None:
        self.session = session
        self.step = _StubStep()

    async def upsert_step(self, task_id, step_name):
        return self.step

    async def set_step_status(self, step, status, message=None):
        step.status = status
        step.message = message


def _make_processor(bus: _CapturingBus, monkeypatch, lanes=None) -> TaskProcessor:
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.lanes = lanes
    proc.bus = bus
    proc.session_factory = lambda: _StubSession()
    ctx = PipelineContext.__new__(PipelineContext)
    ctx.lanes = lanes
    ctx.bus = bus
    ctx.session_factory = proc.session_factory
    proc._ctx = ctx
    return proc


@pytest.mark.asyncio
async def test_run_step_marks_step_completed_not_failed_on_awaiting_input(
    monkeypatch,
) -> None:
    """First-run pause: step raises TaskAwaitingInput -> step status must be
    `completed` (it did its job), and the exception must still propagate so
    the outer pipeline loop can transition the task to awaiting_input."""

    bus = _CapturingBus()
    proc = _make_processor(bus, monkeypatch)

    repo = _RunStepRepo(None)
    session = _StubSession()
    logger = logging.getLogger("test.run_step_awaiting_input")

    class _FakeMatchSpeakersStep(Step):
        name = "match_speakers"
        lane = None

        async def already_done(self, ctx, st) -> bool:
            return False

        async def run(self, ctx, st):
            raise TaskAwaitingInput("match_speakers")

    monkeypatch.setitem(STEP_REGISTRY, "match_speakers", _FakeMatchSpeakersStep())

    task_id = uuid.uuid4()
    with pytest.raises(TaskAwaitingInput) as exc_info:
        await proc._run_step(
            session, repo, task_id, "user-1", "match_speakers", {}, logger, {}
        )

    assert exc_info.value.step == "match_speakers"
    assert repo.step.status == StepStatus.completed, (
        "match_speakers step must be marked completed (not failed) when it "
        "raises TaskAwaitingInput — this is what lets already_done "
        "short-circuit the step on resume"
    )

    step_events = [e for e in bus.events if e.get("event") == "step"]
    assert step_events[-1]["data"]["status"] == StepStatus.completed.value
    assert not any(
        e["data"]["status"] == StepStatus.failed.value for e in step_events
    ), "no failed step event should be published for TaskAwaitingInput"


@pytest.mark.asyncio
async def test_run_step_still_marks_failed_for_real_step_errors(monkeypatch) -> None:
    """Sanity check: the fix must not swallow genuine step failures — only
    TaskAwaitingInput gets the completed treatment."""

    bus = _CapturingBus()
    proc = _make_processor(bus, monkeypatch)

    repo = _RunStepRepo(None)
    session = _StubSession()
    logger = logging.getLogger("test.run_step_real_failure")

    class _BoomStep(Step):
        name = "match_speakers"
        lane = None

        async def already_done(self, ctx, st) -> bool:
            return False

        async def run(self, ctx, st):
            raise RuntimeError("boom")

    monkeypatch.setitem(STEP_REGISTRY, "match_speakers", _BoomStep())

    task_id = uuid.uuid4()
    with pytest.raises(RuntimeError, match="boom"):
        await proc._run_step(
            session, repo, task_id, "user-1", "match_speakers", {}, logger, {}
        )

    assert repo.step.status == StepStatus.failed


@pytest.mark.asyncio
async def test_resume_after_awaiting_input_short_circuits_via_already_done(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end of the resume story at the _run_step level: once the step
    row is `completed` (as the fix now sets it) and speaker_matches.json is
    on disk (written before the pause), a second _run_step call for the same
    step must short-circuit via already_done and NOT invoke run() again --
    i.e. it must not re-pause."""

    from vts.pipeline.steps.speaker_match import MatchSpeakersStep

    bus = _CapturingBus()
    proc = _make_processor(bus, monkeypatch)

    dirs = {"outputs": tmp_path / "outputs"}
    dirs["outputs"].mkdir(parents=True, exist_ok=True)
    (dirs["outputs"] / "speaker_matches.json").write_text(
        json.dumps({"SPEAKER_00": {"outcome": "grey", "speaker_id": None}}),
        encoding="utf-8",
    )

    repo = _RunStepRepo(None)
    repo.step.status = StepStatus.completed  # what the fix leaves behind post-pause
    session = _StubSession()
    logger = logging.getLogger("test.resume_short_circuit")

    run_called = False

    class _SpyMatchSpeakersStep(MatchSpeakersStep):
        async def run(self, ctx, st):
            nonlocal run_called
            run_called = True
            raise AssertionError("run() must not execute when already_done is True")

    monkeypatch.setitem(STEP_REGISTRY, "match_speakers", _SpyMatchSpeakersStep())

    task_id = uuid.uuid4()
    await proc._run_step(session, repo, task_id, "user-1", "match_speakers", dirs, logger, {})

    assert run_called is False, "resumed task must short-circuit past match_speakers"
    # already_done short-circuit returns early without touching step status.
    assert repo.step.status == StepStatus.completed
