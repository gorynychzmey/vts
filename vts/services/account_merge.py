"""Fold one account into another when both belong to the same person (vts-ash6).

The case this exists for: someone's mail domain is renamed. They sign in with
the new address, OAuth does not recognise it, and `get_or_create_user` hands
them an empty account — every task and every recording stays behind on the row
keyed by the old address, and the library looks wiped.

The fix is a rename, not a migration. Identity here *is* `users.username`, so
the row holding the history survives and takes the new address; the empty row
created by the first sign-in gives up whatever it accumulated and is deleted.
Keeping the older row is not an aesthetic choice: API tokens, delivery
credentials and step weights all hang off `users.id`, and moving the history
the other way would invalidate them for no gain.

Tables are discovered from the SQLAlchemy metadata by their foreign key to
`users.id` rather than listed here. A list would be correct on the day it was
written and silently incomplete after the next table is added — and the symptom
of an incomplete list is rows stranded on a deleted user, which nobody sees
until that data is missed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User, UserSession
from vts.services.storage import user_hash

# Sessions are revoked rather than moved, so the move skips this table.
_SESSION_TABLE = UserSession.__tablename__

# Statuses in which nothing is going to write into the artifact directory
# again. Anything else means a worker may be holding paths it resolved before
# the move, so renaming the directory under it is a race.
_TERMINAL_STATUSES = (
    TaskStatus.completed,
    TaskStatus.archived,
    TaskStatus.failed,
    TaskStatus.canceled,
)


@dataclass
class ArtifactMove:
    """The filesystem half of the merge, which cannot join the transaction."""

    source: Path
    destination: Path


@dataclass
class MergeReport:
    """What the merge did — or, before a commit, what it would do."""

    kept_user_id: uuid.UUID
    absorbed_user_id: uuid.UUID | None
    old_username: str
    new_username: str
    # table name -> rows repointed onto the surviving account. Non-zero only.
    moved: dict[str, int] = field(default_factory=dict)
    # table name -> rows deleted because they would have collided with a row
    # the surviving account already owns. Non-zero only.
    dropped: dict[str, int] = field(default_factory=dict)
    sessions_revoked: int = 0
    # table name -> rows whose stored filesystem paths were repointed.
    paths_rewritten: dict[str, int] = field(default_factory=dict)
    artifact_move: ArtifactMove | None = None


def _user_id_columns() -> list[sa.Column]:
    """Every `<table>.user_id`-shaped column pointing at `users.id`."""
    columns: list[sa.Column] = []
    for table in Base.metadata.sorted_tables:
        if table.name == User.__tablename__:
            continue
        for column in table.columns:
            if any(fk.column is User.__table__.c.id for fk in column.foreign_keys):
                columns.append(column)
    return columns


def _unique_keys_involving(column: sa.Column) -> list[tuple[list[sa.Column], object]]:
    """Unique constraints and unique indexes on `column`'s table that include it.

    Returns (other columns, predicate) pairs. `predicate` is the partial-index
    WHERE clause when there is one, else None — `prompts` allows a single
    `is_system` row per user through exactly such an index, and ignoring the
    predicate would read every non-system prompt as a collision.
    """
    table = column.table
    keys: list[tuple[list[sa.Column], object]] = []

    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and any(
            c is column for c in constraint.columns
        ):
            keys.append(([c for c in constraint.columns if c is not column], None))

    for index in table.indexes:
        if not index.unique or not any(c is column for c in index.columns):
            continue
        predicate = index.dialect_options.get("postgresql", {}).get("where")
        keys.append(([c for c in index.columns if c is not column], predicate))

    return keys


async def _drop_colliding_rows(
    session: AsyncSession, column: sa.Column, *, keep: uuid.UUID, absorb: uuid.UUID
) -> int:
    """Delete the absorbed account's rows that the survivor already has.

    Done as two small SELECTs and a DELETE by primary key rather than one
    correlated statement: the partial-index predicate is stored as raw SQL
    (`sa.text("is_system")`), and an unqualified column name inside a
    self-join is ambiguous. Both sides are scoped to a single user, so the
    rows read here are bounded by that user's own data.
    """
    table = column.table
    dropped = 0

    for other_columns, predicate in _unique_keys_involving(column):
        pk = list(table.primary_key.columns)[0]

        def _select(owner: uuid.UUID, lead):
            # `lead` is never part of the key — it is there so the statement
            # has at least one column. A key made of user_id alone (the
            # per-user singletons: the system prompt copy, the step weights)
            # leaves `other_columns` empty, and SELECT with no columns is not
            # a query.
            statement = sa.select(lead, *other_columns).where(column == owner)
            return statement if predicate is None else statement.where(predicate)

        kept_rows = (await session.execute(_select(keep, sa.literal(1)))).all()
        if not kept_rows:
            # Nothing on the surviving side to collide with. Stated separately
            # because for a user_id-only key the mere existence of a kept row
            # IS the collision, and an empty `kept` set would match nothing.
            continue
        kept = {tuple(row[1:]) for row in kept_rows}

        candidates = (await session.execute(_select(absorb, pk))).all()
        doomed = [row[0] for row in candidates if tuple(row[1:]) in kept]
        if not doomed:
            continue

        result = await session.execute(sa.delete(table).where(pk.in_(doomed)))
        dropped += result.rowcount or 0

    return dropped


def _path_columns(table: sa.Table) -> list[sa.Column]:
    """Columns of `table` that hold a filesystem path.

    Recognised by name, because nothing in the metadata marks a column as a
    path. That would be a fragile way to decide anything destructive, but the
    rewrite below only touches values that already sit under the directory
    being renamed — a column caught here by accident is left alone on its own
    contents.
    """
    return [
        column
        for column in table.columns
        if column.name == "artifact_dir" or column.name.endswith("_path")
    ]


async def _assert_nothing_in_flight(session: AsyncSession, owners: list[uuid.UUID]) -> None:
    in_flight = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Task)
        .where(Task.user_id.in_(owners), Task.status.not_in(_TERMINAL_STATUSES))
    )
    if in_flight:
        raise RuntimeError(
            f"{in_flight} task(s) still in flight; artifacts cannot be moved while a "
            "worker may be writing into the directory. Retry once they settle."
        )


async def _rewrite_artifact_paths(
    session: AsyncSession, *, owner: uuid.UUID, artifacts_root: Path, old: str, new: str
) -> dict[str, int]:
    """Repoint stored paths from the old username's directory to the new one."""
    old_prefix = f"{artifacts_root / user_hash(old)}/"
    new_prefix = f"{artifacts_root / user_hash(new)}/"
    rewritten: dict[str, int] = {}

    for column in _user_id_columns():
        table = column.table
        paths = _path_columns(table)
        if not paths:
            continue

        def _repointed(path_column: sa.Column):
            # autoescape: the root is an arbitrary path, and an underscore in
            # it is a single-character wildcard to LIKE.
            return sa.case(
                (
                    path_column.startswith(old_prefix, autoescape=True),
                    new_prefix + sa.func.substr(path_column, len(old_prefix) + 1),
                ),
                else_=path_column,
            )

        result = await session.execute(
            sa.update(table)
            .where(
                column == owner,
                sa.or_(*(p.startswith(old_prefix, autoescape=True) for p in paths)),
            )
            .values({p.name: _repointed(p) for p in paths})
        )
        if result.rowcount:
            rewritten[table.name] = result.rowcount

    return rewritten


async def merge_accounts(
    session: AsyncSession,
    *,
    old_username: str,
    new_username: str,
    artifacts_root: Path | None = None,
) -> MergeReport:
    """Give the `old_username` account the `new_username` address.

    Any account already sitting on `new_username` is absorbed first: its rows
    move onto the survivor, its duplicates are dropped, and it is deleted. When
    no such account exists this degenerates to a rename, which is the same
    operation with nothing to absorb.

    Passing `artifacts_root` additionally repoints the stored paths at the
    directory the new address hashes to, and returns the directory rename to
    perform — see `apply_artifact_move`. Without it the merge is database-only:
    old work keeps reading from the old directory, which is correct but leaves
    the person owning two.

    The caller owns the transaction — nothing here commits. That is what makes
    a dry run possible: run it, read the report, roll back.
    """
    keeper = await session.scalar(sa.select(User).where(User.username == old_username))
    if keeper is None:
        raise LookupError(f"no account exists for {old_username!r}")

    absorbed = await session.scalar(sa.select(User).where(User.username == new_username))
    if absorbed is not None and absorbed.id == keeper.id:
        raise LookupError(f"{old_username!r} and {new_username!r} are the same account")

    report = MergeReport(
        kept_user_id=keeper.id,
        absorbed_user_id=absorbed.id if absorbed else None,
        old_username=old_username,
        new_username=new_username,
    )

    # Sessions go first and unconditionally. `user_sessions.email` is re-read on
    # every request and handed to get_or_create_user (vts/services/auth.py), so
    # a live session carrying the old address would recreate the very account
    # this function deletes — and a rename with no absorbed account would
    # recreate the pre-rename one. Both people affected simply sign in again.
    owners = [keeper.id] + ([absorbed.id] if absorbed else [])

    # Before anything is written: refusing halfway would leave the database
    # moved and the files not.
    if artifacts_root is not None:
        await _assert_nothing_in_flight(session, owners)

    revoked = await session.execute(
        sa.delete(UserSession.__table__).where(UserSession.user_id.in_(owners))
    )
    report.sessions_revoked = revoked.rowcount or 0

    if absorbed is not None:
        for column in _user_id_columns():
            if column.table.name == _SESSION_TABLE:
                continue

            dropped = await _drop_colliding_rows(
                session, column, keep=keeper.id, absorb=absorbed.id
            )
            if dropped:
                report.dropped[column.table.name] = dropped

            moved = await session.execute(
                sa.update(column.table)
                .where(column == absorbed.id)
                .values({column.name: keeper.id})
            )
            if moved.rowcount:
                report.moved[column.table.name] = moved.rowcount

        # Must precede the rename: `users.username` is unique, and the absorbed
        # row is the one currently holding the address the keeper is taking.
        await session.execute(sa.delete(User.__table__).where(User.id == absorbed.id))

    # Assigned through the ORM, not as Core DML: the caller is holding this very
    # object (same session, same identity map), and a Core UPDATE would leave it
    # showing the old address. Expiring the session instead would be worse — in
    # an async session an expired attribute turns every later read into I/O
    # outside the greenlet, which raises somewhere far from here.
    if artifacts_root is not None:
        # After the move, so the rows just absorbed are covered too. Theirs
        # already point at the new directory, and the prefix guard leaves them
        # untouched.
        report.paths_rewritten = await _rewrite_artifact_paths(
            session,
            owner=keeper.id,
            artifacts_root=artifacts_root,
            old=old_username,
            new=new_username,
        )
        report.artifact_move = ArtifactMove(
            source=artifacts_root / user_hash(old_username),
            destination=artifacts_root / user_hash(new_username),
        )

    keeper.username = new_username
    await session.flush()

    return report


def apply_artifact_move(source: Path, destination: Path) -> None:
    """Fold `source` into `destination` on disk.

    Separate from the merge and run after it commits, because a filesystem move
    cannot be rolled back with the transaction. Both directories live under the
    same artifacts root, so every rename here is a metadata operation — the size
    of what is being moved does not matter.

    A missing source is not an error: an account that produced nothing has no
    directory.
    """
    if not source.exists():
        return

    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return

    # Both exist — the normal case, since the person has been using the new
    # address. Collisions are checked up front so a refusal does not leave the
    # directories half-folded.
    children = list(source.iterdir())
    for child in children:
        if (destination / child.name).exists():
            raise FileExistsError(
                f"{destination / child.name} already exists; refusing to overwrite"
            )

    for child in children:
        child.rename(destination / child.name)
    source.rmdir()
