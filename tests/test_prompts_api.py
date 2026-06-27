import uuid

import pytest
from pydantic import ValidationError
from vts.api.schemas import (
    PromptRef, PromptCreateRequest, TaskCreateRequest,
)

from tests.conftest import _TEST_USER_ID


def test_task_create_defaults_to_summary():
    req = TaskCreateRequest(url="https://x/y")
    assert req.prompts == [PromptRef(source="system", id="summary")]


def test_task_create_empty_prompts_allowed_without_summary():
    req = TaskCreateRequest(url="https://x/y", prompts=[])
    assert req.prompts == []


def test_non_empty_prompts_requires_transcript():
    with pytest.raises(ValidationError):
        TaskCreateRequest(url="https://x/y", transcript=False,
                          prompts=[PromptRef(source="system", id="summary")])


def test_prompt_create_request_validates():
    with pytest.raises(ValidationError):
        PromptCreateRequest(name="", system_prompt="x")


# ---------------------------------------------------------------------------
# HTTP-client endpoint tests (use the authed-client `client` fixture).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_includes_system_summary(client):
    resp = await client.get("/api/prompts")
    assert resp.status_code == 200
    body = resp.json()
    assert any(p["source"] == "system" and p["id"] == "summary" for p in body)
    summary = next(p for p in body if p["id"] == "summary")
    assert summary["editable"] is False


@pytest.mark.asyncio
async def test_prompt_create_list_update_delete(client):
    created = (await client.post("/api/prompts",
               json={"name": "Mine", "system_prompt": "Do X"})).json()
    assert created["source"] == "user" and created["editable"] is True
    pid = created["id"]

    listed = (await client.get("/api/prompts")).json()
    assert any(p["id"] == pid for p in listed)

    patched = (await client.patch(f"/api/prompts/{pid}",
               json={"name": "Renamed"})).json()
    assert patched["name"] == "Renamed"

    assert (await client.delete(f"/api/prompts/{pid}")).status_code == 204


class _FakeRedis:
    """Minimal async Redis stub: enough for create_task's RedisBus calls
    (publish) and queue-position cache (get/setex)."""

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


@pytest.mark.asyncio
async def test_create_task_stores_prompts_in_options(authed_app, client, tmp_path, monkeypatch):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    from vts.core.config import get_settings
    get_settings.cache_clear()
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    resp = await client.post("/api/tasks", json={
        "url": "https://example.com/v",
        "prompts": [{"source": "system", "id": "summary"}],
    })
    assert resp.status_code == 200, resp.text
    options = resp.json()["options"]
    assert options["prompts"] == [{"source": "system", "id": "summary"}]
    assert "summary" not in options


@pytest.mark.asyncio
async def test_upload_task_stores_prompts_in_options(authed_app, client, tmp_path, monkeypatch):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    from vts.core.config import get_settings
    get_settings.cache_clear()
    app, _factory = authed_app
    app.state.redis = _FakeRedis()
    resp = await client.post(
        "/api/tasks/upload",
        files={"file": ("clip.mp3", b"fake-audio-bytes", "audio/mpeg")},
        data={"prompts": '[{"source": "system", "id": "summary"}]'},
    )
    assert resp.status_code == 200, resp.text
    options = resp.json()["options"]
    assert options["prompts"] == [{"source": "system", "id": "summary"}]
    assert "summary" not in options


@pytest.mark.asyncio
async def test_get_prompt_result_from_index(authed_app, client, tmp_path):
    """A result registered in options['prompt_results'] is read back as text."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    result_file = tmp_path / "user_result.md"
    result_file.write_text("indexed result body", encoding="utf-8")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={
                "prompts": [{"source": "user", "id": "p1"}],
                "prompt_results": [
                    {"source": "user", "id": "p1", "path": str(result_file)},
                ],
            },
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    resp = await client.get(f"/api/tasks/{task_id}/results/user/p1")
    assert resp.status_code == 200, resp.text
    assert resp.text == "indexed result body"


@pytest.mark.asyncio
async def test_get_prompt_result_system_summary_fallback(authed_app, client, tmp_path):
    """system/summary falls back to task.summary_path when not in the index."""
    _app, factory = authed_app
    from vts.db.models import Task
    from vts.db.repo import Repo

    summary_file = tmp_path / "summary.md"
    summary_file.write_text("the summary text", encoding="utf-8")

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"prompts": [{"source": "system", "id": "summary"}]},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        task.summary_path = str(summary_file)
        await session.commit()

    resp = await client.get(f"/api/tasks/{task_id}/results/system/summary")
    assert resp.status_code == 200, resp.text
    assert resp.text == "the summary text"


@pytest.mark.asyncio
async def test_get_prompt_result_missing_is_404(authed_app, client, tmp_path):
    _app, factory = authed_app
    from vts.db.repo import Repo

    task_id = uuid.uuid4()
    async with factory() as session:
        repo = Repo(session)
        await repo.create_task(
            user_id=uuid.UUID(_TEST_USER_ID),
            source_url="https://example.com/v",
            options={"prompts": []},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    resp = await client.get(f"/api/tasks/{task_id}/results/user/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_prompt_result_unknown_task_is_404(client):
    resp = await client.get(f"/api/tasks/{uuid.uuid4()}/results/system/summary")
    assert resp.status_code == 404
