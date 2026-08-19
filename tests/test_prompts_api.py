import uuid

import pytest
from pydantic import ValidationError
from vts.api.schemas import (
    PromptRef, PromptCreateRequest, TaskCreateRequest,
)

from tests.conftest import _TEST_USER_ID


def test_task_create_defaults_to_summary():
    req = TaskCreateRequest(url="https://example.com/y")
    assert req.prompts == [PromptRef(source="system", id="summary")]


def test_task_create_empty_prompts_allowed_without_summary():
    req = TaskCreateRequest(url="https://example.com/y", prompts=[])
    assert req.prompts == []


def test_non_empty_prompts_requires_transcript():
    with pytest.raises(ValidationError):
        TaskCreateRequest(url="https://example.com/y", transcript=False,
                          prompts=[PromptRef(source="system", id="summary")])


def test_prompt_create_request_validates():
    with pytest.raises(ValidationError):
        PromptCreateRequest(name="", system_prompt="x")


# ---------------------------------------------------------------------------
# HTTP-client endpoint tests (use the authed-client `client` fixture).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_returns_the_system_copy_as_an_editable_user_row(client):
    """The vendor prompt is the user's own row now, not a separate entry.

    It used to be listed as `source="system", editable=False`, unreachable by
    the edit endpoints. Since vts-kujy it is a normal user row the editor can
    write to, told apart only by `is_system` — which is what lets the UI offer
    "Restore" instead of "Delete" for it.
    """
    # Reading the text is what materialises the copy for a user who has never
    # had one; the list endpoint only reports rows that already exist.
    assert (await client.get("/api/prompts/system/summary/text")).status_code == 200

    resp = await client.get("/api/prompts")
    assert resp.status_code == 200
    body = resp.json()

    assert not any(p["source"] == "system" for p in body), (
        "the system prompt must no longer be served as a separate non-user entry"
    )
    system_rows = [p for p in body if p["is_system"]]
    assert len(system_rows) == 1
    summary = system_rows[0]
    assert summary["source"] == "user"
    assert summary["editable"] is True
    assert summary["name"] == "Summary"
    uuid.UUID(summary["id"])  # a real row id, not the "summary" registry key


@pytest.mark.asyncio
async def test_prompts_list_marks_an_ordinary_prompt_as_not_system(client):
    """`is_system` is the frontend's only way to tell the two cases apart, so a
    prompt the user wrote must not carry it."""
    created = (await client.post("/api/prompts",
               json={"name": "Mine", "system_prompt": "Do X"})).json()
    assert created["is_system"] is False

    listed = (await client.get("/api/prompts")).json()
    mine = next(p for p in listed if p["id"] == created["id"])
    assert mine["is_system"] is False


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


@pytest.mark.asyncio
async def test_prompt_update_rejects_blank_name(client):
    created = (await client.post("/api/prompts",
               json={"name": "Mine", "system_prompt": "Do X"})).json()
    pid = created["id"]

    # whitespace-only name → 422
    assert (await client.patch(f"/api/prompts/{pid}",
            json={"name": "   "})).status_code == 422
    # empty name → 422
    assert (await client.patch(f"/api/prompts/{pid}",
            json={"name": ""})).status_code == 422

    # body-only update (no name) → 200, name unchanged
    patched = await client.patch(f"/api/prompts/{pid}",
                                 json={"system_prompt": "new body"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Mine"

    # valid name with surrounding whitespace → 200 and trimmed
    patched = await client.patch(f"/api/prompts/{pid}",
                                 json={"name": "  Renamed  "})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"


# ---------------------------------------------------------------------------
# Detail / system-text endpoints (Task 12 — used by the duplicate feature).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_detail_returns_system_prompt(client):
    created = (await client.post("/api/prompts",
               json={"name": "Detail", "system_prompt": "Body text here"})).json()
    pid = created["id"]

    resp = await client.get(f"/api/prompts/{pid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "user"
    assert body["id"] == pid
    assert body["name"] == "Detail"
    assert body["system_prompt"] == "Body text here"
    assert body["editable"] is True


@pytest.mark.asyncio
async def test_prompt_detail_404_for_missing_id(client):
    missing = uuid.uuid4()
    resp = await client.get(f"/api/prompts/{missing}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_prompt_detail_404_for_other_users_prompt(authed_app, client):
    """A prompt owned by a different user must not be readable."""
    _app, factory = authed_app
    from vts.db.models import User
    from vts.db.repo import Repo

    other_id = uuid.uuid4()
    async with factory() as session:
        session.add(User(id=other_id, username="other"))
        await session.flush()
        repo = Repo(session)
        row = await repo.create_prompt(other_id, "Theirs", "secret body")
        await session.commit()
        other_pid = str(row.id)

    resp = await client.get(f"/api/prompts/{other_pid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_system_prompt_text_returns_file_contents(client, tmp_path, monkeypatch):
    monkeypatch.setenv("VTS_PROMPTS_DIR", str(tmp_path))
    from vts.core.config import get_settings
    get_settings.cache_clear()
    (tmp_path / "global_prompt.md").write_text("SYSTEM SUMMARY BODY", encoding="utf-8")

    resp = await client.get("/api/prompts/system/summary/text")
    assert resp.status_code == 200, resp.text
    assert resp.json()["system_prompt"] == "SYSTEM SUMMARY BODY"


@pytest.mark.asyncio
async def test_system_prompt_text_404_for_unknown_key(client):
    resp = await client.get("/api/prompts/system/nope/text")
    assert resp.status_code == 404


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
