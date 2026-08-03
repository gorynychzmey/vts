from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from tests.mcp.conftest import FakeBus, FakeRepo, FakeUser
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult
from vts.mcp.tools import (
    create_delivery_target,
    delete_delivery_target,
    list_delivery_targets,
    submit_video,
    update_delivery_target,
)

SECRET_VALUE = "s3cr3t-token"


class FakeAdapter:
    name = "fake"

    def config_schema(self) -> dict:
        return {"type": "object"}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    async def deliver(self, payload, target):
        return DeliveryResult()


class _Settings:
    def __init__(self) -> None:
        self.secrets_key = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _registered_adapter(monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", {"fake": FakeAdapter()}, raising=False)


@pytest.fixture
def settings() -> _Settings:
    return _Settings()


async def test_create_target_never_returns_secret_value(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()

    info = await create_delivery_target(
        user=user, repo=repo, settings=settings,
        name="outline-meetings", adapter="fake",
        config={"collection_id": "c1"}, secrets={"api_token": SECRET_VALUE},
    )

    assert SECRET_VALUE not in str(info.model_dump())
    assert info.secrets == {"api_token": {"set": True}}
    assert info.adapter_available is True
    assert info.config == {"collection_id": "c1"}


async def test_list_targets_hides_secret_values(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    await create_delivery_target(
        user=user, repo=repo, settings=settings, name="t1", adapter="fake",
        config={}, secrets={"api_token": SECRET_VALUE},
    )

    listing = await list_delivery_targets(user=user, repo=repo, settings=settings)

    assert len(listing) == 1
    assert SECRET_VALUE not in str([t.model_dump() for t in listing])
    assert listing[0].secrets == {"api_token": {"set": True}}


async def test_create_rejects_unknown_adapter(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await create_delivery_target(
            user=user, repo=repo, settings=settings,
            name="bad", adapter="nope", config={},
        )
    assert exc.value.status_code == 400


async def test_update_keeps_secret_then_clears_it(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    created = await create_delivery_target(
        user=user, repo=repo, settings=settings, name="t", adapter="fake",
        config={"a": 1}, secrets={"api_token": SECRET_VALUE},
    )

    kept = await update_delivery_target(
        user=user, repo=repo, settings=settings, target_id=created.id, config={"a": 2},
    )
    assert kept.config == {"a": 2}
    assert kept.secrets == {"api_token": {"set": True}}

    cleared = await update_delivery_target(
        user=user, repo=repo, settings=settings, target_id=created.id, clear_secrets=True,
    )
    assert cleared.secrets == {"api_token": {"set": False}}


async def test_delete_target_and_404(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    created = await create_delivery_target(
        user=user, repo=repo, settings=settings, name="gone", adapter="fake", config={},
    )
    assert await delete_delivery_target(user=user, repo=repo, target_id=created.id) == {
        "deleted": True
    }
    with pytest.raises(HTTPException) as exc:
        await delete_delivery_target(user=user, repo=repo, target_id=created.id)
    assert exc.value.status_code == 404


async def test_target_with_missing_adapter_still_lists(settings, monkeypatch):
    """Settings survive a plugin that failed to load; the target is flagged."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    await create_delivery_target(
        user=user, repo=repo, settings=settings, name="orphan", adapter="fake",
        config={"x": 1}, secrets={"api_token": SECRET_VALUE},
    )

    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    listing = await list_delivery_targets(user=user, repo=repo, settings=settings)
    assert listing[0].adapter_available is False
    assert listing[0].name == "orphan"
    assert SECRET_VALUE not in str(listing[0].model_dump())


# --------------------------------------------------------------------------
# submit_video(delivery=...)
# --------------------------------------------------------------------------


async def test_submit_with_valid_delivery_lands_in_options(tmp_path: Path, settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    await create_delivery_target(
        user=user, repo=repo, settings=settings, name="out", adapter="fake", config={},
    )

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        delivery=[{"deliver_to": "out", "variant": "raw"}],
    )

    assert repo.tasks[result.task_id].options["delivery"] == [
        {"deliver_to": "out", "variant": "raw"}
    ]


async def test_submit_rejects_unknown_target(tmp_path: Path):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
            delivery=[{"deliver_to": "does-not-exist"}],
        )
    assert exc.value.status_code == 422
    assert "does-not-exist" in str(exc.value.detail)


async def test_submit_rejects_target_whose_adapter_is_unavailable(
    tmp_path: Path, settings, monkeypatch
):
    """Explicit submit fails fast — the caller can act on it right now."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    await create_delivery_target(
        user=user, repo=repo, settings=settings, name="out", adapter="fake", config={},
    )
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
            delivery=[{"deliver_to": "out"}],
        )
    assert exc.value.status_code == 422
    assert "not available" in str(exc.value.detail)


async def test_preset_delivery_survives_missing_adapter(tmp_path: Path, settings, monkeypatch):
    """A preset naming an unavailable target must NOT fail the task.

    The delivery is enqueued and parked in waiting_adapter later; failing the
    submit would make one missing plugin block unrelated transcription work.
    """
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    await create_delivery_target(
        user=user, repo=repo, settings=settings, name="out", adapter="fake", config={},
    )
    preset = await repo.create_preset(
        uuid.UUID(user.id), "cognee", {"delivery": [{"deliver_to": "out"}]}
    )
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
    )

    assert repo.tasks[result.task_id].options["delivery"] == [{"deliver_to": "out"}]


async def test_explicit_delivery_replaces_preset_delivery(tmp_path: Path, settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    for name in ("from-preset", "explicit"):
        await create_delivery_target(
            user=user, repo=repo, settings=settings, name=name, adapter="fake", config={},
        )
    preset = await repo.create_preset(
        uuid.UUID(user.id), "p", {"delivery": [{"deliver_to": "from-preset"}]}
    )

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
        delivery=[{"deliver_to": "explicit"}],
    )

    assert repo.tasks[result.task_id].options["delivery"] == [{"deliver_to": "explicit"}]
