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
    create_delivery_credential,
    create_delivery_target,
    delete_delivery_credential,
    delete_delivery_target,
    list_delivery_credentials,
    list_delivery_targets,
    submit_video,
    update_delivery_credential,
    update_delivery_target,
)

SECRET_VALUE = "s3cr3t-token"


class FakeAdapter:
    name = "fake"
    contract_version = (1, 1)

    def config_schema(self) -> dict:
        return {"type": "object"}

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]

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


async def _conn(user, repo, settings, *, name="conn", secrets=True):
    return await create_delivery_credential(
        user=user, repo=repo, settings=settings, name=name, adapter="fake",
        config={"base_url": "https://o.example/api"},
        secrets={"api_token": SECRET_VALUE} if secrets else None,
    )


async def _dest(user, repo, settings, cred, *, name="out", config=None):
    return await create_delivery_target(
        user=user, repo=repo, settings=settings, name=name, adapter="fake",
        credential_id=cred.id, config=config if config is not None else {},
    )


async def test_create_target_never_returns_secret_value(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()

    cred = await _conn(user, repo, settings)
    assert SECRET_VALUE not in str(cred.model_dump())
    assert cred.secrets == {"api_token": {"set": True}}

    info = await _dest(user, repo, settings, cred,
                       name="outline-meetings", config={"collection_id": "c1"})

    assert SECRET_VALUE not in str(info.model_dump())
    assert info.adapter_available is True
    assert info.config == {"collection_id": "c1"}
    assert info.credential_id == cred.id


async def test_list_targets_hides_secret_values(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    cred = await _conn(user, repo, settings)
    await _dest(user, repo, settings, cred, name="t1")

    listing = await list_delivery_credentials(user=user, repo=repo, settings=settings)

    assert len(listing) == 1
    assert SECRET_VALUE not in str([t.model_dump() for t in listing])
    assert listing[0].secrets == {"api_token": {"set": True}}


async def test_create_rejects_unknown_adapter(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await create_delivery_credential(
            user=user, repo=repo, settings=settings,
            name="bad", adapter="nope", config={},
        )
    assert exc.value.status_code == 400


async def test_update_keeps_secret_then_clears_it(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    created = await _conn(user, repo, settings)

    kept = await update_delivery_credential(
        user=user, repo=repo, settings=settings, credential_id=created.id,
        config={"base_url": "https://other.example/api"},
    )
    assert kept.config == {"base_url": "https://other.example/api"}
    assert kept.secrets == {"api_token": {"set": True}}

    cleared = await update_delivery_credential(
        user=user, repo=repo, settings=settings, credential_id=created.id,
        clear_secrets=True,
    )
    assert cleared.secrets == {"api_token": {"set": False}}


async def test_delete_target_and_404(settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    cred = await _conn(user, repo, settings)
    created = await _dest(user, repo, settings, cred, name="gone")
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
    cred = await _conn(user, repo, settings)
    await _dest(user, repo, settings, cred, name="orphan", config={"x": 1})

    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    listing = await list_delivery_targets(user=user, repo=repo, settings=settings)
    assert listing[0].adapter_available is False
    assert listing[0].name == "orphan"
    assert SECRET_VALUE not in str(listing[0].model_dump())

    creds = await list_delivery_credentials(user=user, repo=repo, settings=settings)
    assert creds[0].adapter_available is False
    assert SECRET_VALUE not in str(creds[0].model_dump())


# --------------------------------------------------------------------------
# submit_video(delivery=...)
# --------------------------------------------------------------------------


async def test_submit_with_valid_delivery_lands_in_options(tmp_path: Path, settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    cred = await _conn(user, repo, settings)
    target = await _dest(user, repo, settings, cred, name="out")

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        delivery=[{"deliver_to": target.id, "variant": "raw"}],
    )

    # Stored by ID, so renaming the target later cannot orphan this task.
    assert repo.tasks[result.task_id].options["delivery"] == [
        {"deliver_to": target.id, "variant": "raw"}
    ]


async def test_submit_rejects_unknown_target(tmp_path: Path):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    missing = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
            delivery=[{"deliver_to": missing}],
        )
    assert exc.value.status_code == 422
    assert missing in str(exc.value.detail)


async def test_submit_rejects_a_name_where_an_id_is_required(tmp_path: Path, settings):
    """deliver_to takes a target ID (vts-929). A name must be refused with a
    clear reason rather than silently looked up, so callers cannot build on a
    reference that a rename would break."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    cred = await _conn(user, repo, settings)
    await _dest(user, repo, settings, cred, name="by-name")

    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://example.com/abc", user=user, repo=repo, bus=bus,
            artifacts_root=tmp_path, delivery=[{"deliver_to": "by-name"}],
        )
    assert exc.value.status_code == 422
    assert "UUID" in str(exc.value.detail)


async def test_submit_rejects_target_whose_adapter_is_unavailable(
    tmp_path: Path, settings, monkeypatch
):
    """Explicit submit fails fast — the caller can act on it right now."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    cred = await _conn(user, repo, settings)
    target = await _dest(user, repo, settings, cred, name="out")
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
            delivery=[{"deliver_to": target.id}],
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
    cred = await _conn(user, repo, settings)
    target = await _dest(user, repo, settings, cred, name="out")
    preset = await repo.create_preset(
        uuid.UUID(user.id), "cognee", {"delivery": [{"deliver_to": target.id}]}
    )
    monkeypatch.setattr(registry, "_CACHE", {}, raising=False)  # plugin gone

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
    )

    assert repo.tasks[result.task_id].options["delivery"] == [{"deliver_to": target.id}]


async def test_explicit_delivery_replaces_preset_delivery(tmp_path: Path, settings):
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    cred = await _conn(user, repo, settings)
    from_preset = await _dest(user, repo, settings, cred, name="from-preset")
    explicit = await _dest(user, repo, settings, cred, name="explicit")
    preset = await repo.create_preset(
        uuid.UUID(user.id), "p", {"delivery": [{"deliver_to": from_preset.id}]}
    )

    result = await submit_video(
        url="https://example.com/abc", user=user, repo=repo, bus=bus, artifacts_root=tmp_path,
        preset={"source": "user", "id": str(preset.id)},
        delivery=[{"deliver_to": explicit.id}],
    )

    assert repo.tasks[result.task_id].options["delivery"] == [{"deliver_to": explicit.id}]
