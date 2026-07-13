import pytest
from pydantic import ValidationError
from vts.api.schemas import RestartSummaryRequest, PromptRef
import uuid

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
    req = RestartSummaryRequest(mode="final_only",
                                prompts=[PromptRef(source="system", id="summary")])
    assert req.prompts == [PromptRef(source="system", id="summary")]

def test_prompts_rejected_with_full():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(mode="full",
                              prompts=[PromptRef(source="system", id="summary")])

def test_empty_prompts_rejected():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(mode="final_only", prompts=[])

def test_none_prompts_ok_any_mode():
    assert RestartSummaryRequest(mode="full").prompts is None
    assert RestartSummaryRequest(mode="final_only").prompts is None


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

    resp = await client.post(f"/api/tasks/{task_id}/restart_summary", json={
        "mode": "final_only",
        "prompts": [{"source":"system","id":"summary"}, {"source":"user","id":pb_id}],
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

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
    resp = await client.post(f"/api/tasks/{uuid.uuid4()}/restart_summary",
        json={"mode":"final_only","prompts":[]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_restart_final_prompts_with_full_422(authed_app, client):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    resp = await client.post(f"/api/tasks/{uuid.uuid4()}/restart_summary",
        json={"mode":"full",
              "prompts":[{"source":"system","id":"summary"}]})
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["final_only", "full"])
async def test_restart_downgrades_stale_summary_result_entry(
    mode, client, authed_app, tmp_path
):
    """Regressions vts-b6l + vts-5eg.

    vts-b6l: restart_summary deletes the summary files but used to leave the
    system:summary prompt_results entry completed with a path to the deleted
    file — the frontend selected it and got 404. It must go pending.

    vts-5eg: mode=full regenerates the processed transcript, so user-prompt
    results are stale too: their entries go pending, their finalize steps
    reset, their result files are deleted (else step_finalize_prompt
    short-circuits on existing files and never regenerates). mode=final_only
    keeps them — their input did not change."""
    app, factory = authed_app
    app.state.redis = _FakeRedis()
    from vts.db.models import Task, TaskStatus, Step, StepStatus

    async with factory() as s:
        uid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
        art = tmp_path / f"task-{mode}"; (art / "summary").mkdir(parents=True)
        (art / "summary" / "final.md").write_text("old")
        results_dir = art / "summary" / "results"; results_dir.mkdir()
        (results_dir / "user__a.md").write_text("old memo")
        (results_dir / "user__a.json").write_text('{"raw": "old memo"}')
        task = Task(id=uuid.uuid4(), user_id=uid, source_url="x",
                    artifact_dir=str(art), status=TaskStatus.completed,
                    summary_path=str(art / "summary" / "final.md"),
                    options={"prompts": [{"source": "system", "id": "summary"},
                                         {"source": "user", "id": "a"}],
                             "prompt_results": [
                                {"source": "system", "id": "summary", "name": "S",
                                 "path": str(art / "summary" / "final.md"),
                                 "status": "completed"},
                                {"source": "user", "id": "a", "name": "A",
                                 "path": str(results_dir / "user__a.md"),
                                 "status": "completed"}]})
        s.add(task)
        for name in ["download", "merge_transcript", "summarize_windows",
                     "summarize_final", "finalize:user:a"]:
            s.add(Step(task_id=task.id, name=name, status=StepStatus.completed))
        await s.commit()
        task_id = str(task.id)

    resp = await client.post(f"/api/tasks/{task_id}/restart_summary",
                             json={"mode": mode})
    assert resp.status_code == 200

    async with factory() as s:
        from vts.db.repo import Repo
        t = await Repo(s).get_task_by_id(uuid.UUID(task_id))
        by_ref = {(e["source"], e["id"]): e for e in t.options["prompt_results"]}
        assert by_ref[("system", "summary")]["status"] == "pending", \
            f"stale system:summary entry must be downgraded, got {by_ref}"
        assert t.summary_path is None
        step_status = {st.name: st.status for st in t.steps}
        if mode == "full":
            # processed transcript is regenerated -> user results are stale
            assert by_ref[("user", "a")]["status"] == "pending"
            assert step_status["finalize:user:a"] == StepStatus.pending
            assert not (results_dir / "user__a.md").exists()
            assert not (results_dir / "user__a.json").exists()
        else:
            # final_only: the user prompt's input did not change
            assert by_ref[("user", "a")]["status"] == "completed"
            assert step_status["finalize:user:a"] == StepStatus.completed
            assert (results_dir / "user__a.md").exists()


@pytest.mark.asyncio
async def test_two_finalize_results_both_persist(authed_app, tmp_path):
    """Regression (vts-jal): a task that finalizes two prompts (system summary +
    one user prompt) must end with BOTH entries in options.prompt_results.

    Mirrors the worker: each finalize step persists its result via its own
    session (upsert_result_entry over a shallow dict(task.options) +
    set_task_prompt_results). The earlier in-place list mutation caused the
    second write to be silently dropped on commit, leaving one result and a
    hidden results dropdown.
    """
    from vts.db.models import Task, TaskStatus
    from vts.db.repo import Repo
    from vts.services.prompt_results import upsert_result_entry

    _app, factory = authed_app
    uid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    async with factory() as s:
        task = Task(id=uuid.uuid4(), user_id=uid, source_url="x",
                    artifact_dir=str(tmp_path), status=TaskStatus.running,
                    options={"prompts": [{"source": "system", "id": "summary"},
                                         {"source": "user", "id": "u1"}]})
        s.add(task)
        await s.commit()
        tid = task.id

    async def persist(source, ref_id, name):
        # Exactly what TaskProcessor._persist_prompt_result does.
        async with factory() as s:
            repo = Repo(s)
            t = await repo.get_task_by_id(tid)
            options = dict(t.options or {})
            entries = upsert_result_entry(options, source, ref_id, name, "/p", "completed")
            await repo.set_task_prompt_results(t, entries)
            await s.commit()

    await persist("system", "summary", "Summary")
    await persist("user", "u1", "My prompt")

    async with factory() as s:
        t = await Repo(s).get_task_by_id(tid)
        results = t.options.get("prompt_results", [])
        assert {(e["source"], e["id"]) for e in results} == {
            ("system", "summary"), ("user", "u1")
        }, f"both finalize results must persist, got {results}"
