import pytest
from pydantic import ValidationError
from vts.api.schemas import RestartSummaryRequest, PromptRef
import uuid

TID = [uuid.uuid4()]


class _FakeRedis:
    """Minimal async Redis stub for RedisBus.notify_queued (publish) and the
    queue-position cache (get/setex)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def publish(self, channel, message) -> int:
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


def test_prompts_allowed_with_final_only():
    req = RestartSummaryRequest(task_ids=TID, mode="final_only",
                                prompts=[PromptRef(source="system", id="summary")])
    assert req.prompts == [PromptRef(source="system", id="summary")]

def test_prompts_rejected_with_full():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="full",
                              prompts=[PromptRef(source="system", id="summary")])

def test_empty_prompts_rejected():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="final_only", prompts=[])

def test_none_prompts_ok_any_mode():
    assert RestartSummaryRequest(task_ids=TID, mode="full").prompts is None
    assert RestartSummaryRequest(task_ids=TID, mode="final_only").prompts is None


@pytest.mark.asyncio
async def test_restart_final_with_new_prompts_rebuilds_tail(client, authed_app, tmp_path, monkeypatch):
    """A completed task with [summary, user:a] restarted final with [summary, user:b]:
    options.prompts becomes the new set, prompt_results cleared, finalize steps
    rebuilt (finalize:user:a deleted, summarize_final + finalize:user:b pending),
    head steps untouched, status queued."""
    app, factory = authed_app
    app.state.redis = _FakeRedis()
    from vts.db.models import Task, TaskStatus, Step, StepStatus, Prompt
    import uuid
    # Seed a user prompt 'b' so the ref is valid, a task with completed head + finals.
    async with factory() as s:
        uid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
        pb = Prompt(id=uuid.uuid4(), user_id=uid, name="B", system_prompt="b")
        s.add(pb)
        art = tmp_path / "task"; (art / "summary").mkdir(parents=True)
        (art / "summary" / "final.md").write_text("old")
        task = Task(id=uuid.uuid4(), user_id=uid, source_url="x",
                    artifact_dir=str(art), status=TaskStatus.completed,
                    summary_path=str(art/"summary"/"final.md"),
                    options={"prompts": [{"source":"system","id":"summary"},
                                         {"source":"user","id":"a"}],
                             "prompt_results": [
                                {"source":"system","id":"summary","name":"S","path":str(art/"summary"/"final.md"),"status":"completed"},
                                {"source":"user","id":"a","name":"A","path":str(art/"summary"/"results"/"user__a.md"),"status":"completed"}]})
        s.add(task)
        for name in ["download","extract_audio","trim_initial_silence","segment_audio",
                     "detect_language","transcribe_segments","merge_transcript",
                     "prepare_llama_model","prepare_summary_chunks","summarize_windows",
                     "pack_window_notes","summarize_final","finalize:user:a"]:
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        task_id = str(task.id); pb_id = str(pb.id)

    resp = await client.post("/api/tasks/restart_summary", json={
        "task_ids": [task_id], "mode": "final_only",
        "prompts": [{"source":"system","id":"summary"}, {"source":"user","id":pb_id}],
    })
    assert resp.status_code == 200
    assert resp.json()["results"][task_id] == "queued"

    async with factory() as s:
        from vts.db.repo import Repo
        t = await Repo(s).get_task_by_id(uuid.UUID(task_id))
        assert t.status == TaskStatus.queued
        assert {tuple(p.values()) for p in t.options["prompts"]} == {
            ("system","summary"), ("user", pb_id)}
        step_status = {st.name: st.status for st in t.steps}
        assert "finalize:user:a" not in step_status                 # removed
        assert step_status[f"finalize:user:{pb_id}"] == StepStatus.pending  # added
        assert step_status["summarize_final"] == StepStatus.pending         # reset
        assert step_status["summarize_windows"] == StepStatus.completed     # head untouched
        assert t.summary_path is None
        assert t.options["prompt_results"] == []


@pytest.mark.asyncio
async def test_restart_final_empty_prompts_422(authed_app, client):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    resp = await client.post("/api/tasks/restart_summary",
        json={"task_ids":[str(uuid.uuid4())],"mode":"final_only","prompts":[]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_restart_final_prompts_with_full_422(authed_app, client):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    resp = await client.post("/api/tasks/restart_summary",
        json={"task_ids":[str(uuid.uuid4())],"mode":"full",
              "prompts":[{"source":"system","id":"summary"}]})
    assert resp.status_code == 422
