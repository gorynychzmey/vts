"""DELETE /api/tasks must leave an audit trail (vts-om71).

Deleting is irreversible — the row is dropped and the artifact directory is
rmtree'd — yet the only trace it used to leave was an access-log line:

    INFO: 2.27.86.100:0 - "DELETE /api/tasks HTTP/1.1" 200 OK

That line carries neither WHO deleted (the access log has no `as_user`, so an
admin acting for someone else is indistinguishable from the user themselves)
nor WHAT was deleted. On 2026-08-24 a user's tasks vanished and the incident
could only be narrowed down to "six DELETEs from one IP" — the task ids and the
acting identity were unrecoverable.

These tests pin the audit record, not the deletion itself.
"""
from __future__ import annotations

import logging
import uuid

import pytest
import pytest_asyncio

from vts.db.models import Task, TaskStatus

from tests.conftest import _TEST_USER_ID

USER_ID = uuid.UUID(_TEST_USER_ID)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value

    async def set(self, key, value, ex=None) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value

    async def publish(self, *_args, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


@pytest_asyncio.fixture
async def seeded(authed_app, tmp_path):
    """Two deletable tasks with real artifact dirs (delete rmtree's them)."""
    _app, factory = authed_app
    async with factory() as session:
        rows = []
        for name in ("alpha", "beta"):
            artifact = tmp_path / name
            artifact.mkdir()
            rows.append(
                Task(
                    id=uuid.uuid4(),
                    user_id=USER_ID,
                    source_url=f"file://{name}.m4a",
                    source_title=f"Recording {name}",
                    status=TaskStatus.completed,
                    artifact_dir=str(artifact),
                )
            )
        for row in rows:
            session.add(row)
        await session.commit()
        return [str(r.id) for r in rows]


@pytest.mark.asyncio
async def test_delete_logs_who_and_what(client, seeded, caplog):
    """The audit line names the actor and every task id it removed."""
    with caplog.at_level(logging.INFO, logger="vts.api.routers.tasks"):
        response = await client.request(
            "DELETE", "/api/tasks", json={"task_ids": seeded}
        )
    assert response.status_code == 200

    records = [r for r in caplog.records if "task.delete" in r.getMessage()]
    assert records, (
        "deleting left no audit record; searched for 'task.delete' in "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    logged = " ".join(r.getMessage() for r in records)
    # WHO: both identities, so impersonated deletes are attributable.
    assert "tester" in logged
    # WHAT: every id, so a restore knows what to look for.
    for task_id in seeded:
        assert task_id in logged, f"task id {task_id} missing from audit record"


@pytest.mark.asyncio
async def test_delete_audit_records_acting_identity(authed_app, client, caplog):
    """An admin deleting via ?as_user= is logged as the admin, not the target.

    This is the distinction the 2026-08-24 incident could not make.
    """
    from vts.api.deps import require_user
    from vts.services.auth import AuthenticatedUser

    app, factory = authed_app

    async def _admin_acting_as_tester() -> AuthenticatedUser:
        return AuthenticatedUser(
            id=_TEST_USER_ID,
            username="tester",
            requested_by="admin@example.com",
            is_admin=True,
            acting_as="tester",
        )

    app.dependency_overrides[require_user] = _admin_acting_as_tester

    async with factory() as session:
        task = Task(
            id=uuid.uuid4(),
            user_id=USER_ID,
            source_url="file://gamma.m4a",
            status=TaskStatus.completed,
            artifact_dir="/tmp/does-not-exist-gamma",
        )
        session.add(task)
        await session.commit()
        task_id = str(task.id)

    with caplog.at_level(logging.INFO, logger="vts.api.routers.tasks"):
        response = await client.request(
            "DELETE", "/api/tasks", json={"task_ids": [task_id]}
        )
    assert response.status_code == 200

    logged = " ".join(
        r.getMessage() for r in caplog.records if "task.delete" in r.getMessage()
    )
    assert "admin@example.com" in logged, (
        "the audit record must name the admin who actually issued the delete"
    )


@pytest.mark.asyncio
async def test_audit_survives_a_failed_delete(authed_app, client, seeded, caplog, monkeypatch):
    """The trail is written BEFORE the row and artifacts go away.

    Logging after the rmtree would lose the record in exactly the case worth
    investigating — a delete that blew up halfway through.
    """
    import vts.api.routers.tasks as tasks_mod

    def _boom(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(tasks_mod.shutil, "rmtree", _boom)

    with caplog.at_level(logging.INFO, logger="vts.api.routers.tasks"):
        with pytest.raises(Exception):
            await client.request("DELETE", "/api/tasks", json={"task_ids": seeded})

    logged = " ".join(
        r.getMessage() for r in caplog.records if "task.delete" in r.getMessage()
    )
    assert logged, "a delete that failed mid-flight left no audit trail at all"
    for task_id in seeded:
        assert task_id in logged
