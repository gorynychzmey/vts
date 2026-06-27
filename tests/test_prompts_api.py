import pytest
from pydantic import ValidationError
from vts.api.schemas import (
    PromptRef, PromptCreateRequest, TaskCreateRequest,
)


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
