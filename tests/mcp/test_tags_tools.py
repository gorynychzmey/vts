"""MCP tools carry tags (vts-qwcj).

These run against the real Postgres-backed Repo: tag behaviour (normalisation,
case-insensitive keys, per-user isolation) lives in SQL, and a fake would only
repeat what it was told.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastmcp import Client
from sqlalchemy import select

from tests.conftest import _TEST_USER_ID
from tests.mcp.conftest import FakeBus, FakeUser
from vts.db.models import Task, User
from vts.db.repo import Repo
from vts.mcp import tools

pytestmark = pytest.mark.asyncio

USER_ID = uuid.UUID(_TEST_USER_ID)
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a4")


@pytest.fixture
def factory(authed_app):
    return authed_app[1]


@pytest.fixture
def user() -> FakeUser:
    return FakeUser(id=str(USER_ID), username="tester")


@pytest.fixture
def mcp_client(factory, monkeypatch, user):
    """An in-process MCP client whose tools talk to the test database."""
    import vts.mcp.server as server_mod
    import vts.mcp.tools_registry.presets as presets_mod
    import vts.mcp.tools_registry.recordings as recordings_mod
    import vts.mcp.tools_registry.tasks as tasks_mod
    from vts.core.config import Settings

    async def _authenticate(_session):
        return user, Settings()

    @asynccontextmanager
    async def _session_cm():
        async with factory() as session:
            yield session

    for mod in (presets_mod, recordings_mod, tasks_mod):
        monkeypatch.setattr(mod, "mcp_authenticate", _authenticate)
        monkeypatch.setattr(mod, "get_db_session_factory", lambda: _session_cm)
    return Client(server_mod.build_mcp_server())


async def _make(factory, title, tags, *, user_id=USER_ID):
    """A listed recording with its task, carrying `tags`."""
    async with factory() as s:
        repo = Repo(s)
        d = f"/tmp/mcp-tags-{uuid.uuid4()}"
        task = await repo.create_task(
            user_id=user_id, source_url=f"https://example.com/{title}",
            options={}, artifact_dir=d,
        )
        task.source_title = title
        await repo.set_task_content(task, transcript_path=f"{d}/transcript.txt")
        rec = await repo.upsert_recording_for_task(task)
        if tags:
            await repo.set_recording_tags(rec, tags)
        await s.commit()
        return task.id, rec.id


@pytest.fixture
async def corpus(factory):
    async with factory() as s:
        s.add(User(id=OTHER_ID, username="other-mcp"))
        await s.commit()
    a = await _make(factory, "alpha", ["Work", "Home"])
    b = await _make(factory, "beta", ["work"])
    c = await _make(factory, "gamma", [])
    await _make(factory, "delta", ["work", "home"], user_id=OTHER_ID)
    return {"alpha": a, "beta": b, "gamma": c}


# ---------------------------------------------------------------- submit_video

async def test_submit_video_sets_normalised_tags_on_the_new_recording(factory, user, tmp_path):
    async with factory() as s:
        result = await tools.submit_video(
            url="https://example.com/v", user=user, repo=Repo(s), bus=FakeBus(),
            artifacts_root=tmp_path, tags=["  Work ", "work", "Deep   Dive"],
        )
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        rec = await repo.recording_for_task(result.task_id)
        names = [t.name for t in (await repo.tags_for_recordings([rec.id])).get(rec.id, [])]
    # Deduplicated by key, first spelling kept, sorted by key.
    assert names == ["Deep Dive", "Work"]


async def test_submit_video_rejects_a_bad_tag_before_creating_anything(factory, user, tmp_path):
    async with factory() as s:
        with pytest.raises(HTTPException) as exc:
            await tools.submit_video(
                url="https://example.com/v", user=user, repo=Repo(s), bus=FakeBus(),
                artifacts_root=tmp_path, tags=["fine", "x" * 65],
            )
        assert exc.value.status_code == 422
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        assert await repo.list_tags(USER_ID) == []
        assert (await s.scalars(select(Task))).first() is None
    assert list(tmp_path.iterdir()) == []


async def test_submit_video_without_tags_is_unchanged(factory, user, tmp_path):
    async with factory() as s:
        result = await tools.submit_video(
            url="https://example.com/v", user=user, repo=Repo(s), bus=FakeBus(),
            artifacts_root=tmp_path,
        )
        await s.commit()
    async with factory() as s:
        assert await Repo(s).list_tags(USER_ID) == []
        assert result.status == "queued"


# ------------------------------------------------------------------ list_tasks

async def test_list_tasks_filters_by_tags_and_reports_them(factory, user, corpus):
    async with factory() as s:
        page = await tools.list_tasks(user=user, repo=Repo(s), tags=["work", "HOME"])
        assert [t.title for t in page.tasks] == ["alpha"]
        assert page.total == 1
        assert page.tasks[0].tags == ["Home", "Work"]

        anyp = await tools.list_tasks(user=user, repo=Repo(s), tags=["home", "work"], tag_match="any")
        assert {t.title for t in anyp.tasks} == {"alpha", "beta"}
        assert anyp.total == 2

        everything = await tools.list_tasks(user=user, repo=Repo(s))
        by_title = {t.title: t.tags for t in everything.tasks}
        assert by_title == {"alpha": ["Home", "Work"], "beta": ["Work"], "gamma": []}


async def test_list_tasks_rejects_an_unknown_tag_match(factory, user, corpus):
    async with factory() as s:
        with pytest.raises(HTTPException) as exc:
            await tools.list_tasks(user=user, repo=Repo(s), tags=["work"], tag_match="some")
        assert exc.value.status_code == 422


# ------------------------------------------------------------- list_recordings

async def test_list_recordings_filters_by_tags_and_reports_them(mcp_client, corpus):
    async with mcp_client as client:
        both = await client.call_tool("list_recordings", {"tags": ["work", "home"]})
        assert [i.title for i in both.data.items] == ["alpha"]
        assert both.data.total == 1
        assert both.data.items[0].tags == ["Home", "Work"]

        anyr = await client.call_tool(
            "list_recordings", {"tags": ["home", "work"], "tag_match": "any"}
        )
        assert {i.title for i in anyr.data.items} == {"alpha", "beta"}
        assert anyr.data.total == 2

        unknown = await client.call_tool("list_recordings", {"tags": ["nope"]})
        assert unknown.data.items == [] and unknown.data.total == 0

        plain = await client.call_tool("list_recordings", {})
        assert {i.title: i.tags for i in plain.data.items} == {
            "alpha": ["Home", "Work"], "beta": ["Work"], "gamma": [],
        }


async def test_list_recordings_tag_filter_keeps_task_scoped_filters(mcp_client, corpus):
    """A tag filter and `task_ids` must both hold, not one overriding the other."""
    alpha_task, _ = corpus["alpha"]  # beta also has "work" but is not asked for
    async with mcp_client as client:
        result = await client.call_tool(
            "list_recordings", {"tags": ["work"], "task_ids": [str(alpha_task)]}
        )
        assert [i.title for i in result.data.items] == ["alpha"]


# ------------------------------------------------------------------- list_tags

async def test_list_tags_returns_names_with_counts(mcp_client, factory, corpus):
    async with factory() as s:
        await Repo(s).resolve_tags(USER_ID, ["unused"], create=True)
        await s.commit()
    async with mcp_client as client:
        result = await client.call_tool("list_tags", {})
    # Only this user's tags; the other user's "work"/"home" do not count here.
    assert [(t.name, t.count) for t in result.data] == [
        ("Home", 1), ("unused", 0), ("Work", 2),
    ]


# ---------------------------------------------------------------- tag_recording

async def test_tag_recording_adds_and_removes_and_returns_the_tags_after(mcp_client, factory, corpus):
    _, alpha = corpus["alpha"]
    async with mcp_client as client:
        r = await client.call_tool(
            "tag_recording",
            {"recording_id": str(alpha), "add": ["  Fresh  "], "remove": ["HOME", "never-had"]},
        )
        assert r.data == ["Fresh", "Work"]
        # A second call sees the first one's result: it persisted.
        r2 = await client.call_tool("tag_recording", {"recording_id": str(alpha), "add": ["fresh"]})
        assert r2.data == ["Fresh", "Work"]
    async with factory() as s:
        # `remove` of a name nobody has creates nothing.
        assert "never-had" not in [t.name for t, _ in await Repo(s).list_tags(USER_ID)]


async def test_tag_recording_unknown_or_foreign_recording_is_not_found(mcp_client, factory, corpus):
    _, foreign = await _make(factory, "foreign", ["work"], user_id=OTHER_ID)
    async with mcp_client as client:
        for rid in (uuid.uuid4(), foreign):
            r = await client.call_tool(
                "tag_recording", {"recording_id": str(rid), "add": ["x"]}, raise_on_error=False
            )
            assert r.is_error is True
            assert "not found" in str(r.content).lower()
    async with factory() as s:
        # Nothing was created for the failed calls.
        assert [t.name for t, _ in await Repo(s).list_tags(USER_ID)] == ["Home", "Work"]


async def test_tag_recording_rejects_an_invalid_name_and_changes_nothing(mcp_client, factory, corpus):
    _, alpha = corpus["alpha"]
    async with mcp_client as client:
        r = await client.call_tool(
            "tag_recording",
            {"recording_id": str(alpha), "add": ["ok", "x" * 65]},
            raise_on_error=False,
        )
        assert r.is_error is True
    async with factory() as s:
        repo = Repo(s)
        assert "ok" not in [t.name for t, _ in await repo.list_tags(USER_ID)]
        tags = (await repo.tags_for_recordings([alpha])).get(alpha, [])
        assert [t.name for t in tags] == ["Home", "Work"]


# --------------------------------------------------------------------- presets

async def test_create_preset_keeps_only_the_users_existing_tag_ids(mcp_client, factory, corpus):
    async with factory() as s:
        repo = Repo(s)
        mine = (await repo.resolve_tags(USER_ID, ["Work"], create=False))[0]
        theirs = (await repo.resolve_tags(OTHER_ID, ["work"], create=False))[0]
    async with mcp_client as client:
        created = await client.call_tool("create_preset", {
            "name": "p",
            "options": {"language": "en", "tags": [str(mine.id), str(theirs.id), "junk", str(uuid.uuid4())]},
        })
    assert created.data.options["tags"] == [str(mine.id)]
    async with factory() as s:
        stored = (await Repo(s).list_presets(USER_ID))[0].options
    assert stored["tags"] == [str(mine.id)]


async def test_update_preset_filters_tag_ids_too(mcp_client, factory, corpus):
    async with factory() as s:
        repo = Repo(s)
        mine = (await repo.resolve_tags(USER_ID, ["Work"], create=False))[0]
        row = await repo.create_preset(USER_ID, "p", {"language": "en"})
        await s.commit()
        preset_id = row.id
    async with mcp_client as client:
        updated = await client.call_tool("update_preset", {
            "preset_id": str(preset_id), "options": {"tags": [str(mine.id), "junk"]},
        })
    assert updated.data.options["tags"] == [str(mine.id)]
    async with factory() as s:
        assert (await Repo(s).get_preset(USER_ID, preset_id)).options["tags"] == [str(mine.id)]


async def test_list_presets_omits_deleted_tags_and_system_presets_have_none(mcp_client, factory, corpus):
    async with factory() as s:
        repo = Repo(s)
        mine = (await repo.resolve_tags(USER_ID, ["Work"], create=False))[0]
        # Stored directly: a tag deleted after the preset was saved.
        await repo.create_preset(USER_ID, "p", {"tags": [str(mine.id), str(uuid.uuid4())]})
        await s.commit()
    async with mcp_client as client:
        result = await client.call_tool("list_presets", {})
    by_source = {}
    for p in result.data:
        by_source.setdefault(p.source, []).append(p)
    assert all(p.options["tags"] == [] for p in by_source["system"])
    assert [p.options["tags"] for p in by_source["user"]] == [[str(mine.id)]]


async def test_a_preset_without_tags_reads_with_an_empty_list(mcp_client, factory, corpus):
    async with factory() as s:
        await Repo(s).create_preset(USER_ID, "plain", {"language": "en"})
        await s.commit()
    async with mcp_client as client:
        result = await client.call_tool("list_presets", {})
    user_presets = [p for p in result.data if p.source == "user"]
    assert user_presets[0].options["tags"] == []


# ------------------------------------------------- submit_video with a preset

async def _submit_with_preset(factory, user, tmp_path, preset_tags, **kw):
    async with factory() as s:
        repo = Repo(s)
        row = await repo.create_preset(USER_ID, "p", {"tags": preset_tags})
        await s.commit()
        ref = {"source": "user", "id": str(row.id)}
    async with factory() as s:
        repo = Repo(s)
        result = await tools.submit_video(
            url="https://example.com/v", user=user, repo=repo, bus=FakeBus(),
            artifacts_root=tmp_path, preset=ref, **kw,
        )
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        rec = await repo.recording_for_task(result.task_id)
        return [t.name for t in (await repo.tags_for_recordings([rec.id])).get(rec.id, [])]


async def _tag_ids(factory, *names):
    async with factory() as s:
        repo = Repo(s)
        out = [str(t.id) for t in await repo.resolve_tags(USER_ID, list(names), create=True)]
        await s.commit()
        return out


async def test_preset_tags_apply_when_tags_are_omitted(factory, user, tmp_path):
    ids = await _tag_ids(factory, "Work", "Alpha")
    assert await _submit_with_preset(factory, user, tmp_path, ids) == ["Alpha", "Work"]


async def test_explicit_empty_tags_override_the_preset(factory, user, tmp_path):
    ids = await _tag_ids(factory, "Work")
    assert await _submit_with_preset(factory, user, tmp_path, ids, tags=[]) == []


async def test_explicit_tags_win_over_the_preset(factory, user, tmp_path):
    ids = await _tag_ids(factory, "Work")
    assert await _submit_with_preset(factory, user, tmp_path, ids, tags=["Other"]) == ["Other"]


async def test_a_preset_tag_deleted_meanwhile_is_skipped(factory, user, tmp_path):
    ids = await _tag_ids(factory, "Work", "Gone")
    async with factory() as s:
        repo = Repo(s)
        await repo.delete_tag(USER_ID, uuid.UUID(ids[1]))
        await s.commit()
    got = await _submit_with_preset(factory, user, tmp_path, [*ids, "junk"])
    assert got == ["Work"]


async def test_a_system_preset_adds_no_tags(factory, user, tmp_path):
    async with factory() as s:
        result = await tools.submit_video(
            url="https://example.com/v", user=user, repo=Repo(s), bus=FakeBus(),
            artifacts_root=tmp_path, preset={"source": "system", "id": "default"},
        )
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        rec = await repo.recording_for_task(result.task_id)
        assert (await repo.tags_for_recordings([rec.id])) == {}
