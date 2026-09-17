"""Merging two accounts that belong to the same person (vts-ash6).

The case these cover: someone's mail domain is renamed, they sign in with the
new address, and OAuth hands them a brand-new empty account while all their
history stays behind on the old one.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import (
    Prompt,
    Recording,
    Task,
    TaskStatus,
    User,
    UserSession,
)
from vts.services.account_merge import apply_artifact_move, merge_accounts
from vts.services.storage import user_hash

from _db import make_test_engine

OLD = "someone@old-domain.example"
NEW = "someone@new-domain.example"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as sess:
            yield sess
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _user(session: AsyncSession, username: str) -> User:
    user = User(id=uuid.uuid4(), username=username)
    session.add(user)
    await session.flush()
    return user


async def _task(session: AsyncSession, user: User, *, status=TaskStatus.completed) -> Task:
    task = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        source_url="https://example.com/v",
        status=status,
        options={},
        artifact_dir=f"/artifacts/{uuid.uuid4()}",
    )
    session.add(task)
    await session.flush()
    return task


async def _recording(session: AsyncSession, user: User, task: Task) -> Recording:
    recording = Recording(
        id=uuid.uuid4(),
        user_id=user.id,
        source_task_id=task.id,
        title="rec",
        artifact_dir=task.artifact_dir,
    )
    session.add(recording)
    await session.flush()
    return recording


async def _count_for(session: AsyncSession, model, user_id: uuid.UUID) -> int:
    return await session.scalar(
        sa.select(sa.func.count()).select_from(model).where(model.user_id == user_id)
    )


# ---------------------------------------------------------------------------
# The core move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_repoints_tasks_of_the_new_account_onto_the_old_one(session):
    """The survivor is the account holding the history, so the handful of rows
    created under the new address move onto it — not the other way round."""
    old = await _user(session, OLD)
    new = await _user(session, NEW)
    for _ in range(3):
        await _task(session, old)
    await _task(session, new)

    await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert await _count_for(session, Task, old.id) == 4


@pytest.mark.asyncio
async def test_merge_repoints_the_library_of_the_new_account(session):
    old = await _user(session, OLD)
    new = await _user(session, NEW)
    old_task = await _task(session, old)
    new_task = await _task(session, new)
    await _recording(session, old, old_task)
    await _recording(session, new, new_task)

    await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert await _count_for(session, Recording, old.id) == 2


@pytest.mark.asyncio
async def test_merge_gives_the_surviving_account_the_new_address(session):
    old = await _user(session, OLD)
    await _user(session, NEW)

    await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert await session.scalar(sa.select(User.username).where(User.id == old.id)) == NEW


@pytest.mark.asyncio
async def test_merge_deletes_the_absorbed_account(session):
    await _user(session, OLD)
    new = await _user(session, NEW)

    await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert await session.scalar(sa.select(User).where(User.id == new.id)) is None


@pytest.mark.asyncio
async def test_merge_keeps_the_surviving_user_id_so_api_tokens_survive(session):
    """Tokens, delivery credentials and step weights hang off the user id. The
    merge must not invalidate them, which is the whole reason the older row is
    the one that survives."""
    old = await _user(session, OLD)
    await _user(session, NEW)
    old_id = old.id

    report = await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert report.kept_user_id == old_id


# ---------------------------------------------------------------------------
# Degenerate case: nobody has signed in with the new address yet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_without_a_second_account_is_a_plain_rename(session):
    old = await _user(session, OLD)
    await _task(session, old)

    report = await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert report.absorbed_user_id is None
    assert await session.scalar(sa.select(User.username).where(User.id == old.id)) == NEW
    assert await _count_for(session, Task, old.id) == 1


@pytest.mark.asyncio
async def test_merge_refuses_when_the_old_address_has_no_account(session):
    await _user(session, NEW)

    with pytest.raises(LookupError):
        await merge_accounts(session, old_username=OLD, new_username=NEW)


# ---------------------------------------------------------------------------
# Sessions: leaving one alive resurrects the deleted account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_revokes_the_sessions_of_both_accounts(session):
    """`user_sessions.email` is read on every request and fed straight to
    get_or_create_user (vts/services/auth.py). A surviving row carrying the old
    address would recreate the account we just deleted, so both accounts' rows
    go."""
    old = await _user(session, OLD)
    new = await _user(session, NEW)
    for user, email in ((old, OLD), (new, NEW)):
        session.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                email=email,
                sid_hash=uuid.uuid4().hex,
                issued_at=0,
                expires_at=sa.func.now(),
            )
        )
    await session.flush()

    report = await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert report.sessions_revoked == 2
    assert await session.scalar(sa.select(sa.func.count()).select_from(UserSession)) == 0


# ---------------------------------------------------------------------------
# Unique-constraint collisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_drops_the_absorbed_system_prompt_copy(session):
    """ix_prompts_one_system_per_user allows exactly one is_system row per
    user. Both accounts have one, so the absorbed copy is dropped rather than
    moved — it is a vendor copy, identical to the survivor's."""
    old = await _user(session, OLD)
    new = await _user(session, NEW)
    for user in (old, new):
        session.add(
            Prompt(
                id=uuid.uuid4(),
                user_id=user.id,
                name="Summary",
                system_prompt="...",
                is_system=True,
            )
        )
    await session.flush()

    report = await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert await _count_for(session, Prompt, old.id) == 1
    assert report.dropped["prompts"] == 1


# ---------------------------------------------------------------------------
# Artifacts on disk
#
# task_dir() names a user's directory sha256(username)[:24], so a rename moves
# where NEW work lands while the stored absolute paths keep pointing at the old
# name. Nothing breaks — paths are read from the database — but the person then
# owns two directories, which is what --move-artifacts is for.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_rewrite_repoints_stored_paths_at_the_new_directory(session, tmp_path):
    old = await _user(session, OLD)
    task = await _task(session, old)
    task.artifact_dir = str(tmp_path / user_hash(OLD) / str(task.id))
    task.transcript_path = f"{task.artifact_dir}/outputs/transcript.txt"
    task.summary_path = f"{task.artifact_dir}/outputs/summary.md"
    await session.flush()

    await merge_accounts(
        session, old_username=OLD, new_username=NEW, artifacts_root=tmp_path
    )

    expected = str(tmp_path / user_hash(NEW) / str(task.id))
    row = (
        await session.execute(
            sa.select(Task.artifact_dir, Task.transcript_path, Task.summary_path).where(
                Task.id == task.id
            )
        )
    ).one()
    assert row.artifact_dir == expected
    assert row.transcript_path == f"{expected}/outputs/transcript.txt"
    assert row.summary_path == f"{expected}/outputs/summary.md"


@pytest.mark.asyncio
async def test_artifact_rewrite_reports_how_many_paths_it_touched(session, tmp_path):
    old = await _user(session, OLD)
    task = await _task(session, old)
    task.artifact_dir = str(tmp_path / user_hash(OLD) / str(task.id))
    await session.flush()
    await _recording(session, old, task)

    report = await merge_accounts(
        session, old_username=OLD, new_username=NEW, artifacts_root=tmp_path
    )

    assert report.paths_rewritten == {"tasks": 1, "recordings": 1}


@pytest.mark.asyncio
async def test_artifact_rewrite_leaves_paths_outside_the_old_directory_alone(session, tmp_path):
    """Only the user's own directory is renamed. A path pointing anywhere else
    was not derived from the username and must not be rewritten by prefix."""
    old = await _user(session, OLD)
    task = await _task(session, old)
    task.artifact_dir = "/somewhere/else/entirely"
    await session.flush()

    await merge_accounts(
        session, old_username=OLD, new_username=NEW, artifacts_root=tmp_path
    )

    stored = await session.scalar(sa.select(Task.artifact_dir).where(Task.id == task.id))
    assert stored == "/somewhere/else/entirely"


@pytest.mark.asyncio
async def test_artifact_rewrite_refuses_while_a_task_is_in_flight(session, tmp_path):
    """Renaming a directory under a running pipeline is a race: the worker holds
    paths it resolved before the move."""
    old = await _user(session, OLD)
    await _task(session, old, status=TaskStatus.running)

    with pytest.raises(RuntimeError, match="in flight"):
        await merge_accounts(
            session, old_username=OLD, new_username=NEW, artifacts_root=tmp_path
        )


@pytest.mark.asyncio
async def test_merge_without_artifacts_root_ignores_an_in_flight_task(session, tmp_path):
    """The database-only merge touches no files, so a running task is no reason
    to refuse it."""
    old = await _user(session, OLD)
    await _task(session, old, status=TaskStatus.running)

    report = await merge_accounts(session, old_username=OLD, new_username=NEW)

    assert report.artifact_move is None


# ---------------------------------------------------------------------------
# The filesystem half, which runs outside the transaction
# ---------------------------------------------------------------------------


def test_apply_artifact_move_renames_when_the_destination_is_absent(tmp_path):
    source = tmp_path / "old-hash"
    (source / "task-a").mkdir(parents=True)
    (source / "task-a" / "media.mp4").write_text("x")

    apply_artifact_move(source, tmp_path / "new-hash")

    assert (tmp_path / "new-hash" / "task-a" / "media.mp4").read_text() == "x"
    assert not source.exists()


def test_apply_artifact_move_folds_into_an_existing_destination(tmp_path):
    """The destination already exists whenever the person created something
    under the new address before the merge — which is the normal case."""
    source = tmp_path / "old-hash"
    (source / "task-a").mkdir(parents=True)
    destination = tmp_path / "new-hash"
    (destination / "task-b").mkdir(parents=True)

    apply_artifact_move(source, destination)

    assert sorted(p.name for p in destination.iterdir()) == ["task-a", "task-b"]
    assert not source.exists()


def test_apply_artifact_move_refuses_to_overwrite_a_name_present_in_both(tmp_path):
    source = tmp_path / "old-hash"
    (source / "task-a").mkdir(parents=True)
    destination = tmp_path / "new-hash"
    (destination / "task-a").mkdir(parents=True)

    with pytest.raises(FileExistsError):
        apply_artifact_move(source, destination)


def test_apply_artifact_move_is_a_no_op_when_the_source_is_absent(tmp_path):
    """A merge whose user never produced anything has nothing to move, and that
    is not an error."""
    apply_artifact_move(tmp_path / "missing", tmp_path / "new-hash")

    assert not (tmp_path / "new-hash").exists()


@pytest.mark.asyncio
async def test_artifact_rewrite_counts_only_the_rows_it_actually_repointed(session, tmp_path):
    """A path that was never under the old directory is not a rewrite, and must
    not be counted as one — otherwise the dry run overstates what a commit will
    touch, which is the number an operator decides on."""
    old = await _user(session, OLD)
    inside = await _task(session, old)
    inside.artifact_dir = str(tmp_path / user_hash(OLD) / str(inside.id))
    outside = await _task(session, old)
    outside.artifact_dir = "/somewhere/else/entirely"
    await session.flush()

    report = await merge_accounts(
        session, old_username=OLD, new_username=NEW, artifacts_root=tmp_path
    )

    assert report.paths_rewritten == {"tasks": 1}
