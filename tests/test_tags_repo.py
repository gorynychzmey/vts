"""Tags on recordings: normalisation and the repo layer (vts-qwcj)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import RecordingTag, Tag, User
from vts.db.repo import Repo
from vts.services.tags import (
    TagExists,
    TagNameError,
    dedupe_names,
    normalize_tag_name,
    tag_key,
)

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tagger"))
        s.add(User(id=_OTHER, username="other"))
        await s.commit()
    f.engine = engine  # for query counting
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _rec(session, user_id=_USER, n=0):
    repo = Repo(session)
    task = await repo.create_task(
        user_id=user_id, source_url=f"https://example.com/{n}",
        options={}, artifact_dir=f"/tmp/tag-{n}-{uuid.uuid4()}",
    )
    return await repo.upsert_recording_for_task(task)


@pytest.mark.parametrize("raw,expected", [
    (" Бизнес  клуб ", "Бизнес клуб"),
    ("a\t\nb", "a b"),
    ("x" * 64, "x" * 64),
])
def test_normalize(raw, expected):
    assert normalize_tag_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "x" * 65])
def test_normalize_rejects(raw):
    with pytest.raises(TagNameError):
        normalize_tag_name(raw)


def test_tag_key_casefolds():
    assert tag_key(" БИЗНЕС-Клуб ") == "бизнес-клуб"
    assert tag_key("Straße") == "strasse"


def test_dedupe_keeps_first_spelling_and_order():
    assert dedupe_names(["Бизнес-клуб", " work ", "бизнес-клуб", "Work"]) == ["Бизнес-клуб", "work"]


async def test_set_recording_tags_dedupes_and_reuses(factory):
    async with factory() as s:
        repo = Repo(s)
        r1, r2 = await _rec(s, n=1), await _rec(s, n=2)
        tags = await repo.set_recording_tags(r1, ["Бизнес-клуб", "бизнес-клуб", "Work"])
        assert sorted(t.name for t in tags) == ["Work", "Бизнес-клуб"]
        again = await repo.set_recording_tags(r2, ["БИЗНЕС-КЛУБ"])
        assert [t.id for t in again] == [next(t.id for t in tags if t.name == "Бизнес-клуб")]
        assert again[0].name == "Бизнес-клуб"
        await s.commit()
        assert len((await s.scalars(select(Tag))).all()) == 2


async def test_set_replaces_the_set(factory):
    async with factory() as s:
        repo = Repo(s)
        r = await _rec(s)
        await repo.set_recording_tags(r, ["a", "b"])
        await repo.set_recording_tags(r, ["b", "c"])
        got = (await repo.tags_for_recordings([r.id]))[r.id]
        assert [t.name for t in got] == ["b", "c"]
        await repo.set_recording_tags(r, [])
        assert (await repo.tags_for_recordings([r.id])).get(r.id, []) == []


async def test_list_tags_counts_including_unlisted_and_unused(factory):
    async with factory() as s:
        repo = Repo(s)
        r1, r2 = await _rec(s, n=1), await _rec(s, n=2)
        assert r1.listed_at is None  # no text yet: unlisted, still counted
        await repo.set_recording_tags(r1, ["b", "a"])
        await repo.set_recording_tags(r2, ["a"])
        await repo.create_tag(_USER, "unused")
        await repo.create_tag(_OTHER, "foreign")
        rows = await repo.list_tags(_USER)
        assert [(t.name, n) for t, n in rows] == [("a", 2), ("b", 1), ("unused", 0)]


async def test_create_tag_collision(factory):
    async with factory() as s:
        repo = Repo(s)
        await repo.create_tag(_USER, "Work")
        with pytest.raises(TagExists):
            await repo.create_tag(_USER, " work ")
        # the same name is free for another user
        assert (await repo.create_tag(_OTHER, "Work")).user_id == _OTHER


async def test_rename_tag(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_tag(_USER, "alpha")
        await repo.create_tag(_USER, "beta")
        with pytest.raises(TagExists):
            await repo.rename_tag(_USER, a.id, "BETA")
        renamed = await repo.rename_tag(_USER, a.id, "ALPHA")  # case-only
        assert renamed.name == "ALPHA" and renamed.name_key == "alpha"
        assert await repo.rename_tag(_OTHER, a.id, "zzz") is None
        assert await repo.rename_tag(_USER, uuid.uuid4(), "zzz") is None


async def test_delete_tag(factory):
    async with factory() as s:
        repo = Repo(s)
        r1, r2 = await _rec(s, n=1), await _rec(s, n=2)
        (tag,) = await repo.set_recording_tags(r1, ["x"])
        await repo.set_recording_tags(r2, ["x", "keep"])
        await s.commit()
        assert await repo.delete_tag(_OTHER, tag.id) is None
        assert await repo.delete_tag(_USER, uuid.uuid4()) is None
        assert await repo.delete_tag(_USER, tag.id) == 2
        await s.commit()
        left = (await s.scalars(select(RecordingTag))).all()
        assert len(left) == 1
        assert [t.name for t, _ in await repo.list_tags(_USER)] == ["keep"]


async def test_change_recording_tags(factory):
    async with factory() as s:
        repo = Repo(s)
        r = await _rec(s)
        await repo.set_recording_tags(r, ["a", "b"])
        got = await repo.change_recording_tags(r, add=["A", "c"], remove=["unknown"])
        assert [t.name for t in got] == ["a", "b", "c"]
        got = await repo.change_recording_tags(r, add=[], remove=["B"])
        assert [t.name for t in got] == ["a", "c"]


async def test_resolve_tags(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_tag(_USER, "Alpha")
        assert await repo.resolve_tags(_USER, ["ALPHA", "nope"], create=False) == [a]
        got = await repo.resolve_tags(_USER, ["ALPHA", "nope", "NOPE"], create=True)
        assert [t.name for t in got] == ["Alpha", "nope"]


async def test_tags_for_recordings_one_query_sorted(factory):
    async with factory() as s:
        repo = Repo(s)
        r1, r2, r3 = await _rec(s, n=1), await _rec(s, n=2), await _rec(s, n=3)
        await repo.set_recording_tags(r1, ["b", "a"])
        await repo.set_recording_tags(r2, ["c"])
        await s.commit()
        statements: list[str] = []

        def count(conn, cursor, statement, *a):
            statements.append(statement)

        event.listen(factory.engine.sync_engine, "before_cursor_execute", count)
        try:
            got = await repo.tags_for_recordings([r1.id, r2.id, r3.id])
        finally:
            event.remove(factory.engine.sync_engine, "before_cursor_execute", count)
        assert len(statements) == 1
        assert [t.name for t in got[r1.id]] == ["a", "b"]
        assert [t.name for t in got[r2.id]] == ["c"]
        assert r3.id not in got
        assert await repo.tags_for_recordings([]) == {}


async def test_casefold_expansion_fits_the_key(factory):
    # "ß" casefolds to "ss": a valid 40-char name has an 80-char key.
    async with factory() as s:
        tag = await Repo(s).create_tag(_USER, "ß" * 40)
        await s.commit()
        assert tag.name_key == "ss" * 40


@pytest.mark.parametrize("op", ["create", "rename"])
async def test_unique_violation_becomes_tag_exists(factory, monkeypatch, op):
    from sqlalchemy import text

    async with factory() as s:
        repo = Repo(s)
        victim = await repo.create_tag(_USER, "other")
        target = await repo.create_tag(_USER, "src") if op == "rename" else None
        await s.commit()

        async def blind(user_id, key):
            # Another request wins the race between our check and our flush.
            await s.execute(text(
                "INSERT INTO tags (id, user_id, name, name_key, created_at) "
                "VALUES (:i, :u, 'Racer', 'racer', now())"
            ), {"i": uuid.uuid4(), "u": user_id})
            return None

        monkeypatch.setattr(repo, "_tag_by_key", blind)
        with pytest.raises(TagExists):
            if op == "create":
                await repo.create_tag(_USER, "RACER")
            else:
                await repo.rename_tag(_USER, target.id, "RACER")
        monkeypatch.undo()
        # the outer session is still usable and the racer's row survived
        assert {t.name for t, _ in await repo.list_tags(_USER)} == {"other", "src", "Racer"} - (
            {"src"} if op == "create" else set())
