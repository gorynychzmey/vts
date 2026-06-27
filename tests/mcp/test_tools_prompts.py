from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.mcp.conftest import FakeRepo, FakeTask, FakeUser
from vts.mcp import tools


# --------------------------------------------------------------------------
# get_prompt_result (replaces the old get_summary tool)
# --------------------------------------------------------------------------


async def test_get_prompt_result_system_summary(tmp_path: Path) -> None:
    """system:summary falls back to task.summary_path via resolve_result_path."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    summary = tmp_path / "summary.md"
    summary.write_text("# Summary\nbody", encoding="utf-8")
    t = FakeTask(
        id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x",
        summary_path=str(summary),
    )
    repo.tasks[t.id] = t

    res = await tools.get_prompt_result(task_id=t.id, ref="system:summary", user=user, repo=repo)
    assert res.content.startswith("# Summary")
    assert res.source == "system"
    assert res.id == "summary"
    assert res.task_id == t.id


async def test_get_prompt_result_user_prompt(tmp_path: Path) -> None:
    """user:<id> resolves from options['prompt_results']."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    result_file = tmp_path / "out.md"
    result_file.write_text("user prompt output", encoding="utf-8")
    pid = str(uuid.uuid4())
    t = FakeTask(
        id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x",
        options={
            "prompt_results": [
                {"source": "user", "id": pid, "name": "n", "path": str(result_file), "status": "done"}
            ]
        },
    )
    repo.tasks[t.id] = t

    res = await tools.get_prompt_result(task_id=t.id, ref=f"user:{pid}", user=user, repo=repo)
    assert res.content == "user prompt output"
    assert res.source == "user"
    assert res.id == pid


async def test_get_prompt_result_task_not_found() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.get_prompt_result(task_id=uuid.uuid4(), ref="system:summary", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_prompt_result_missing(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x")
    repo.tasks[t.id] = t
    with pytest.raises(HTTPException) as exc:
        await tools.get_prompt_result(task_id=t.id, ref="system:summary", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_prompt_result_file_missing(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    t = FakeTask(
        id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x",
        summary_path=str(tmp_path / "missing.md"),
    )
    repo.tasks[t.id] = t
    with pytest.raises(HTTPException) as exc:
        await tools.get_prompt_result(task_id=t.id, ref="system:summary", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_prompt_result_bad_ref(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x")
    repo.tasks[t.id] = t
    with pytest.raises(HTTPException) as exc:
        await tools.get_prompt_result(task_id=t.id, ref="bogus:x", user=user, repo=repo)
    assert exc.value.status_code == 422


def test_get_summary_tool_removed() -> None:
    assert not hasattr(tools, "get_summary")


# --------------------------------------------------------------------------
# list_prompts / create / update / delete
# --------------------------------------------------------------------------


async def test_list_prompts_combines_system_and_user() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    await repo.create_prompt(uuid.UUID(user.id), "My prompt", "do stuff")

    out = await tools.list_prompts(user=user, repo=repo)
    sources = [(p.source, p.editable) for p in out]
    # system prompt(s) first, then user prompt
    assert ("system", False) in sources
    assert ("user", True) in sources
    system = next(p for p in out if p.source == "system")
    assert system.id == "summary"
    user_p = next(p for p in out if p.source == "user")
    assert user_p.name == "My prompt"


async def test_create_prompt() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    info = await tools.create_prompt(
        name="  Greeter  ", system_prompt="say hi", user=user, repo=repo
    )
    assert info.source == "user"
    assert info.editable is True
    assert info.name == "Greeter"
    assert uuid.UUID(info.id) in repo.prompts


async def test_create_prompt_rejects_blank_name() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.create_prompt(name="   ", system_prompt="x", user=user, repo=repo)
    assert exc.value.status_code == 422


async def test_update_prompt() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    row = await repo.create_prompt(uuid.UUID(user.id), "old", "body")
    info = await tools.update_prompt(
        prompt_id=row.id, name="new", system_prompt="newbody", user=user, repo=repo
    )
    assert info.name == "new"
    assert repo.prompts[row.id].system_prompt == "newbody"


async def test_update_prompt_not_found() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.update_prompt(prompt_id=uuid.uuid4(), name="x", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_delete_prompt() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    row = await repo.create_prompt(uuid.UUID(user.id), "old", "body")
    res = await tools.delete_prompt(prompt_id=row.id, user=user, repo=repo)
    assert res == {"deleted": True, "id": str(row.id)}
    assert row.id not in repo.prompts


async def test_delete_prompt_not_found() -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.delete_prompt(prompt_id=uuid.uuid4(), user=user, repo=repo)
    assert exc.value.status_code == 404
