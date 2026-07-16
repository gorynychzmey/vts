from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.mcp.conftest import FakeBus, FakeRepo, FakeUser
from vts.mcp import tools


@pytest.fixture
def fake_user() -> FakeUser:
    return FakeUser(id=str(uuid.uuid4()), username="alice")


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


# --------------------------------------------------------------------------
# submit_video preset expansion
# --------------------------------------------------------------------------


async def test_submit_video_with_user_preset_expands_options(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    preset = await repo.create_preset(
        uuid.UUID(fake_user.id),
        "mypreset",
        {
            "language": "en",
            "audio_only": False,
            "transcript": True,
            "prompts": [{"source": "system", "id": "summary"}],
        },
    )
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
    )
    opts = repo.last_options
    assert opts["language"] == "en"
    assert opts["prompts"] == [{"source": "system", "id": "summary"}]
    assert opts["transcript"] is True
    assert opts["audio_only"] is False


async def test_submit_video_with_system_default_preset(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
        preset={"source": "system", "id": "default"},
    )
    opts = repo.last_options
    assert opts["language"] is None
    assert opts["prompts"] == [{"source": "system", "id": "summary"}]
    assert opts["transcript"] is True


async def test_submit_video_caller_params_override_preset(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    preset = await repo.create_preset(
        uuid.UUID(fake_user.id),
        "mypreset",
        {"language": "en", "audio_only": False, "transcript": True,
         "prompts": [{"source": "system", "id": "summary"}]},
    )
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
        language="de",
        preset={"source": "user", "id": str(preset.id)},
    )
    assert repo.last_options["language"] == "de"


async def test_submit_video_user_preset_not_found(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.submit_video(
            url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
            artifacts_root=tmp_path,
            preset={"source": "user", "id": str(uuid.uuid4())},
        )
    assert exc.value.status_code == 404


async def test_submit_video_unknown_system_preset(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.submit_video(
            url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
            artifacts_root=tmp_path,
            preset={"source": "system", "id": "nope"},
        )
    assert exc.value.status_code == 404


async def test_submit_video_no_preset_unchanged(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
    )
    assert repo.last_options == {
        "language": None,
        "audio_only": False,
        "transcript": True,
        "diarize": False,
        "prompts": [{"source": "system", "id": "summary"}],
    }


async def test_submit_video_with_preset_diarize_survives_expansion(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    """A preset saved with diarize=True must still request diarization when
    consumed via submit_video(preset=...) — this is the exact chain that
    silently dropped the flag before expand_preset_options copied it."""
    repo = FakeRepo()
    preset = await repo.create_preset(
        uuid.UUID(fake_user.id),
        "diarized",
        {"transcript": True, "diarize": True, "prompts": [{"source": "system", "id": "summary"}]},
    )
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
    )
    assert repo.last_options["diarize"] is True


async def test_submit_video_preset_drops_unknown_user_prompt(
    fake_user: FakeUser, fake_bus: FakeBus, tmp_path: Path
) -> None:
    repo = FakeRepo()
    preset = await repo.create_preset(
        uuid.UUID(fake_user.id),
        "mypreset",
        {"transcript": True, "prompts": [{"source": "user", "id": str(uuid.uuid4())}]},
    )
    await tools.submit_video(
        url="https://x/y", user=fake_user, repo=repo, bus=fake_bus,
        artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
    )
    # unknown user prompt id is filtered out
    assert repo.last_options["prompts"] == []


# --------------------------------------------------------------------------
# preset CRUD tools
# --------------------------------------------------------------------------


async def test_list_presets_system_first_then_user(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    await repo.create_preset(uuid.UUID(fake_user.id), "mine", {"language": "ru"})
    out = await tools.list_presets(user=fake_user, repo=repo)
    assert out[0].source == "system"
    assert out[0].id == "default"
    assert out[0].editable is False
    user_presets = [p for p in out if p.source == "user"]
    assert len(user_presets) == 1
    assert user_presets[0].name == "mine"
    assert user_presets[0].editable is True
    assert user_presets[0].options == {"language": "ru"}


async def test_create_preset(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    info = await tools.create_preset(
        name="  P  ", options={"language": "en"}, user=fake_user, repo=repo
    )
    assert info.source == "user"
    assert info.name == "P"
    assert info.options == {"language": "en"}
    assert uuid.UUID(info.id) in repo.presets


async def test_create_preset_rejects_blank_name(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.create_preset(name="  ", options={}, user=fake_user, repo=repo)
    assert exc.value.status_code == 422


async def test_update_preset(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    row = await repo.create_preset(uuid.UUID(fake_user.id), "old", {"language": "en"})
    info = await tools.update_preset(
        preset_id=row.id, name="new", options={"language": "ru"}, user=fake_user, repo=repo
    )
    assert info.name == "new"
    assert repo.presets[row.id].options == {"language": "ru"}


async def test_update_preset_not_found(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.update_preset(preset_id=uuid.uuid4(), name="x", user=fake_user, repo=repo)
    assert exc.value.status_code == 404


async def test_delete_preset(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    row = await repo.create_preset(uuid.UUID(fake_user.id), "p", {})
    res = await tools.delete_preset(preset_id=row.id, user=fake_user, repo=repo)
    assert res == {"deleted": True, "id": str(row.id)}
    assert row.id not in repo.presets


async def test_delete_preset_not_found(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.delete_preset(preset_id=uuid.uuid4(), user=fake_user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_default_preset_falls_back_to_system(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    ref = await tools.get_default_preset(user=fake_user, repo=repo)
    assert ref == {"source": "system", "id": "default"}


async def test_set_default_preset_system(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    ref = await tools.set_default_preset(
        source="system", id="default", user=fake_user, repo=repo
    )
    assert ref == {"source": "system", "id": "default"}
    assert await tools.get_default_preset(user=fake_user, repo=repo) == ref


async def test_set_default_preset_unknown_system(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.set_default_preset(source="system", id="nope", user=fake_user, repo=repo)
    assert exc.value.status_code == 404


async def test_set_default_preset_user(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    row = await repo.create_preset(uuid.UUID(fake_user.id), "p", {})
    ref = await tools.set_default_preset(
        source="user", id=str(row.id), user=fake_user, repo=repo
    )
    assert ref == {"source": "user", "id": str(row.id)}


async def test_set_default_preset_user_not_found(fake_user: FakeUser) -> None:
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await tools.set_default_preset(
            source="user", id=str(uuid.uuid4()), user=fake_user, repo=repo
        )
    assert exc.value.status_code == 404
