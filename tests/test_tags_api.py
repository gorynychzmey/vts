"""Tags over HTTP: the tags API, tags on cards, tags at creation, tags in
presets (vts-qwcj)."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, func, select

from tests.conftest import _TEST_USER_ID
from vts.db.models import Recording, Tag, Task, User
from vts.db.repo import Repo

pytestmark = pytest.mark.asyncio

USER_ID = uuid.UUID(_TEST_USER_ID)
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")


class _FakeRedis:
    """Enough for RedisBus.notify_queued / publish and the queue cache."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def publish(self, channel, message) -> int:
        return 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value


@pytest.fixture(autouse=True)
def _tmp_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    yield


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


@pytest.fixture
def factory(authed_app):
    return authed_app[1]


async def _other_user(factory) -> None:
    async with factory() as s:
        s.add(User(id=OTHER_ID, username="other"))
        await s.commit()


async def _recording(factory, *, user_id=USER_ID, tags=(), n=None, tmp=None) -> tuple[Task, Recording]:
    """A task with a listed recording, optionally tagged."""
    n = n if n is not None else uuid.uuid4().hex[:8]
    async with factory() as s:
        repo = Repo(s)
        artifact_dir = f"/tmp/tags-api-{n}-{uuid.uuid4()}"
        task = await repo.create_task(
            user_id=user_id, source_url=f"https://example.com/{n}",
            options={}, artifact_dir=artifact_dir,
        )
        await repo.set_task_content(task, transcript_path=f"{artifact_dir}/transcript.txt")
        recording = await repo.upsert_recording_for_task(task)
        if tags:
            await repo.set_recording_tags(recording, list(tags))
        await s.commit()
        return task, recording


def _names(tags: list[dict]) -> list[str]:
    return [t["name"] for t in tags]


# ---------------------------------------------------------------------------
# /api/tags
# ---------------------------------------------------------------------------


async def test_create_list_rename_delete(client, factory):
    r = await client.post("/api/tags", json={"name": "  Бизнес   клуб "})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "Бизнес клуб"
    assert set(created) == {"id", "name"}

    await client.post("/api/tags", json={"name": "alpha"})
    await _recording(factory, tags=["бизнес клуб"])
    listed = (await client.get("/api/tags")).json()
    assert listed == [
        {"id": listed[0]["id"], "name": "alpha", "count": 0},
        {"id": created["id"], "name": "Бизнес клуб", "count": 1},
    ]

    r = await client.patch(f"/api/tags/{created['id']}", json={"name": "Club"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": created["id"], "name": "Club"}

    r = await client.delete(f"/api/tags/{created['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {"unlinked": 1}
    assert _names((await client.get("/api/tags")).json()) == ["alpha"]


async def test_create_duplicate_key_is_409(client):
    assert (await client.post("/api/tags", json={"name": "Work"})).status_code == 201
    r = await client.post("/api/tags", json={"name": " WORK "})
    assert r.status_code == 409
    first = (await client.get("/api/tags")).json()[0]
    # The body names the tag in the way, so a client needs no second lookup.
    assert r.json() == {
        "detail": "Tag already exists",
        "tag": {"id": first["id"], "name": "Work"},
    }


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
async def test_create_invalid_name_is_422(client, name):
    r = await client.post("/api/tags", json={"name": name})
    assert r.status_code == 422
    assert (await client.get("/api/tags")).json() == []


async def test_rename_to_existing_key_is_409_case_change_allowed(client):
    a = (await client.post("/api/tags", json={"name": "alpha"})).json()
    await client.post("/api/tags", json={"name": "beta"})
    r = await client.patch(f"/api/tags/{a['id']}", json={"name": "BETA"})
    assert r.status_code == 409
    assert r.json() == {"detail": "Tag already exists"}
    r = await client.patch(f"/api/tags/{a['id']}", json={"name": "ALPHA"})
    assert r.status_code == 200
    assert r.json()["name"] == "ALPHA"


async def test_rename_invalid_name_is_422(client):
    a = (await client.post("/api/tags", json={"name": "alpha"})).json()
    r = await client.patch(f"/api/tags/{a['id']}", json={"name": "  "})
    assert r.status_code == 422


async def test_unknown_tag_is_404(client):
    missing = uuid.uuid4()
    assert (await client.patch(f"/api/tags/{missing}", json={"name": "x"})).status_code == 404
    assert (await client.delete(f"/api/tags/{missing}")).status_code == 404


async def test_another_users_tag_is_404_and_untouched(client, factory):
    """Review Focus 4: never cross-user access."""
    await _other_user(factory)
    async with factory() as s:
        foreign = await Repo(s).create_tag(OTHER_ID, "secret")
        await s.commit()
    assert (await client.patch(f"/api/tags/{foreign.id}", json={"name": "mine"})).status_code == 404
    assert (await client.delete(f"/api/tags/{foreign.id}")).status_code == 404
    assert (await client.get("/api/tags")).json() == []
    async with factory() as s:
        row = await s.get(Tag, foreign.id)
        assert row is not None and row.name == "secret"


# ---------------------------------------------------------------------------
# Setting a recording's tags
# ---------------------------------------------------------------------------


async def test_put_recording_tags_replaces_the_set(client, factory):
    _task, rec = await _recording(factory, tags=["old", "keep"])
    r = await client.put(f"/api/recordings/{rec.id}/tags", json={"names": ["Keep", "new", "NEW"]})
    assert r.status_code == 200, r.text
    assert _names(r.json()) == ["keep", "new"]
    card = (await client.get(f"/api/recordings/{rec.id}")).json()
    assert _names(card["tags"]) == ["keep", "new"]
    # "old" still exists as an empty tag.
    assert {t["name"]: t["count"] for t in (await client.get("/api/tags")).json()} == {
        "keep": 1, "new": 1, "old": 0,
    }


async def test_put_recording_tags_invalid_name_is_422_and_changes_nothing(client, factory):
    _task, rec = await _recording(factory, tags=["keep"])
    r = await client.put(f"/api/recordings/{rec.id}/tags", json={"names": ["fresh", "x" * 65]})
    assert r.status_code == 422
    card = (await client.get(f"/api/recordings/{rec.id}")).json()
    assert _names(card["tags"]) == ["keep"]
    assert _names((await client.get("/api/tags")).json()) == ["keep"]


async def test_put_tags_on_another_users_recording_is_404(client, factory):
    await _other_user(factory)
    task, rec = await _recording(factory, user_id=OTHER_ID)
    assert (await client.put(f"/api/recordings/{rec.id}/tags", json={"names": ["x"]})).status_code == 404
    assert (await client.put(f"/api/tasks/{task.id}/tags", json={"names": ["x"]})).status_code == 404
    assert (await client.get("/api/tags")).json() == []


async def test_put_task_tags_sets_the_recordings_tags(client, factory):
    task, rec = await _recording(factory)
    r = await client.put(f"/api/tasks/{task.id}/tags", json={"names": ["b", "a"]})
    assert r.status_code == 200, r.text
    assert _names(r.json()) == ["a", "b"]
    assert _names((await client.get(f"/api/recordings/{rec.id}")).json()["tags"]) == ["a", "b"]
    assert _names((await client.get(f"/api/tasks/{task.id}")).json()["tags"]) == ["a", "b"]
    assert (await client.put(f"/api/tasks/{uuid.uuid4()}/tags", json={"names": ["x"]})).status_code == 404


# ---------------------------------------------------------------------------
# Cards carry tags
# ---------------------------------------------------------------------------


async def test_cards_carry_tags_sorted_by_key(client, factory):
    task, rec = await _recording(factory, tags=["beta", "Alpha"])
    untagged_task, _ = await _recording(factory)

    tasks = {t["id"]: t for t in (await client.get("/api/tasks")).json()}
    assert _names(tasks[str(task.id)]["tags"]) == ["Alpha", "beta"]
    assert tasks[str(untagged_task.id)]["tags"] == []

    compact = {t["id"]: t for t in (await client.get("/api/tasks?compact=true")).json()}
    assert _names(compact[str(task.id)]["tags"]) == ["Alpha", "beta"]

    filtered = (await client.get("/api/tasks?q=example")).json()
    assert _names(next(t for t in filtered if t["id"] == str(task.id))["tags"]) == ["Alpha", "beta"]

    library = (await client.get("/api/recordings")).json()["items"]
    by_id = {r["id"]: r for r in library}
    assert _names(by_id[str(rec.id)]["tags"]) == ["Alpha", "beta"]
    assert set(by_id[str(rec.id)]["tags"][0]) == {"id", "name"}

    assert _names((await client.get(f"/api/tasks/{task.id}")).json()["tags"]) == ["Alpha", "beta"]
    renamed = await client.patch(f"/api/tasks/{task.id}", json={"display_name": "New name"})
    assert _names(renamed.json()["tags"]) == ["Alpha", "beta"]
    renamed_rec = await client.patch(f"/api/recordings/{rec.id}", json={"display_name": "R"})
    assert _names(renamed_rec.json()["tags"]) == ["Alpha", "beta"]


async def _count_selects(authed_app, client, url: str) -> int:
    _app, factory = authed_app
    engine = factory.kw["bind"]
    seen: list[str] = []

    def _on(conn, cursor, statement, *args):
        if "recording_tags" in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _on)
    try:
        r = await client.get(url)
        assert r.status_code == 200, r.text
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _on)
    return len(seen)


async def test_lists_load_tags_in_one_query(authed_app, client, factory):
    for i in range(4):
        await _recording(factory, tags=[f"t{i}", "shared"])
    assert await _count_selects(authed_app, client, "/api/tasks") == 1
    assert await _count_selects(authed_app, client, "/api/tasks?compact=true") == 1
    assert await _count_selects(authed_app, client, "/api/recordings") == 1


async def test_deleting_a_tag_used_by_three_recordings(client, factory):
    """Review Focus 2: links gone everywhere, cards stop showing it, presets
    drop the id."""
    recs = [await _recording(factory, tags=["doomed", "stays"]) for _ in range(3)]
    tags = {t["name"]: t for t in (await client.get("/api/tags")).json()}
    preset = (await client.post("/api/presets", json={
        "name": "P", "options": {"tags": [tags["doomed"]["id"], tags["stays"]["id"]]},
    })).json()
    assert preset["options"]["tags"] == [tags["doomed"]["id"], tags["stays"]["id"]]

    r = await client.delete(f"/api/tags/{tags['doomed']['id']}")
    assert r.json() == {"unlinked": 3}

    for task, rec in recs:
        assert _names((await client.get(f"/api/recordings/{rec.id}")).json()["tags"]) == ["stays"]
        assert _names((await client.get(f"/api/tasks/{task.id}")).json()["tags"]) == ["stays"]
    for t in (await client.get("/api/tasks")).json():
        assert _names(t["tags"]) == ["stays"]
    for r_ in (await client.get("/api/recordings")).json()["items"]:
        assert _names(r_["tags"]) == ["stays"]

    presets = {p["id"]: p for p in (await client.get("/api/presets")).json()}
    assert presets[preset["id"]]["options"]["tags"] == [tags["stays"]["id"]]


# ---------------------------------------------------------------------------
# Tags at creation
# ---------------------------------------------------------------------------


async def _task_rows(factory) -> int:
    async with factory() as s:
        return int(await s.scalar(select(func.count()).select_from(Task)))


async def test_post_task_with_tags(client, factory):
    r = await client.post("/api/tasks", json={
        "url": "https://example.com/v", "tags": ["Work", " work ", "Club"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert _names(body["tags"]) == ["Club", "Work"]
    assert "tags" not in body["options"]
    async with factory() as s:
        task = await s.get(Task, uuid.UUID(body["id"]))
        assert "tags" not in task.options
    assert {t["name"]: t["count"] for t in (await client.get("/api/tags")).json()} == {
        "Club": 1, "Work": 1,
    }


async def test_post_task_invalid_tag_creates_nothing(client, factory, tmp_path):
    r = await client.post("/api/tasks", json={"url": "https://example.com/v", "tags": ["ok", ""]})
    assert r.status_code == 422
    assert await _task_rows(factory) == 0
    assert (await client.get("/api/tags")).json() == []
    root = tmp_path / "artifacts"
    assert not root.exists() or list(root.rglob("*")) == []


async def test_post_task_without_tags_still_works(client):
    r = await client.post("/api/tasks", json={"url": "https://example.com/v"})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == []


async def test_single_shot_upload_with_tags(client, factory):
    files = {"file": ("clip.mp4", b"abcdef", "video/mp4")}
    r = await client.post("/api/tasks/upload", files=files,
                          data={"transcript": "true", "tags": json.dumps(["b", "A"])})
    assert r.status_code == 200, r.text
    assert _names(r.json()["tags"]) == ["A", "b"]


async def test_single_shot_upload_invalid_tags_creates_nothing(client, factory):
    files = {"file": ("clip.mp4", b"abcdef", "video/mp4")}
    for bad in (json.dumps(["x" * 65]), "not json", json.dumps({"a": 1})):
        r = await client.post("/api/tasks/upload", files=files,
                              data={"transcript": "true", "tags": bad})
        assert r.status_code == 422, (bad, r.text)
    assert await _task_rows(factory) == 0
    assert (await client.get("/api/tags")).json() == []


async def test_chunked_upload_tags_applied_at_finalize(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "clip.mp4", "total_size": 6, "transcript": True, "tags": ["Zeta", "alpha"],
    })
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    # Nothing is created before finalize.
    assert (await client.get("/api/tags")).json() == []
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"abcdef")
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text
    assert _names(fin.json()["tags"]) == ["alpha", "Zeta"]
    assert "tags" not in fin.json()["options"]
    # The idempotent replay returns the same tags.
    again = await client.post(f"/api/uploads/{uid}/finalize")
    assert _names(again.json()["tags"]) == ["alpha", "Zeta"]


async def test_upload_init_invalid_tag_is_422(client):
    r = await client.post("/api/uploads/init", json={
        "filename": "clip.mp4", "total_size": 6, "transcript": True, "tags": ["  "],
    })
    assert r.status_code == 422
    r = await client.post("/api/uploads/init", json={
        "files": [{"filename": "a.m4a", "total_size": 3}], "tags": ["x" * 65],
    })
    assert r.status_code == 422


def _make_audio(path: Path, freq: int) -> bytes:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration=1:sample_rate=16000", "-ac", "1", str(path)],
        capture_output=True, check=True,
    )
    return path.read_bytes()


async def test_multi_file_upload_tags_applied_at_finalize(client, tmp_path):
    a = _make_audio(tmp_path / "a.m4a", 440)
    b = _make_audio(tmp_path / "b.m4a", 880)
    init = await client.post("/api/uploads/init", json={
        "files": [{"filename": "a.m4a", "total_size": len(a)},
                  {"filename": "b.m4a", "total_size": len(b)}],
        "tags": ["set"],
    })
    assert init.status_code == 200, init.text
    uid = init.json()["upload_id"]
    for index, payload in enumerate((a, b)):
        r = await client.patch(f"/api/uploads/{uid}?offset=0&index={index}", content=payload)
        assert r.status_code == 200, r.text
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text
    assert _names(fin.json()["tags"]) == ["set"]


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


async def test_system_presets_carry_no_tags(client):
    for p in (await client.get("/api/presets")).json():
        if p["source"] == "system":
            assert p["options"]["tags"] == []


async def test_preset_save_keeps_only_own_existing_tag_ids(client, factory):
    """Review Focus 4: another user's tag id is dropped from a preset save."""
    await _other_user(factory)
    mine = (await client.post("/api/tags", json={"name": "mine"})).json()
    async with factory() as s:
        foreign = await Repo(s).create_tag(OTHER_ID, "theirs")
        await s.commit()
    wanted = [mine["id"], str(foreign.id), str(uuid.uuid4()), "not-a-uuid"]
    r = await client.post("/api/presets", json={"name": "P", "options": {"tags": wanted}})
    assert r.status_code == 200, r.text
    assert r.json()["options"]["tags"] == [mine["id"]]
    pid = r.json()["id"]
    async with factory() as s:
        from vts.db.models import Preset
        row = await s.get(Preset, uuid.UUID(pid))
        assert row.options["tags"] == [mine["id"]]

    r = await client.patch(f"/api/presets/{pid}", json={"options": {"tags": wanted[::-1]}})
    assert r.status_code == 200, r.text
    assert r.json()["options"]["tags"] == [mine["id"]]

    # A name-only update leaves the stored tags alone.
    r = await client.patch(f"/api/presets/{pid}", json={"name": "Q"})
    assert r.json()["options"]["tags"] == [mine["id"]]


async def test_preset_read_drops_ids_stored_before_the_tag_was_deleted(client, factory):
    """A stored id that is no longer this user's tag is omitted on read, also
    for rows written before the filter existed."""
    a = (await client.post("/api/tags", json={"name": "a"})).json()
    async with factory() as s:
        repo = Repo(s)
        await repo.create_preset(USER_ID, "Legacy", {"language": None})
        await repo.create_preset(USER_ID, "Stale", {"tags": [a["id"], str(uuid.uuid4()), "junk"]})
        await s.commit()
    presets = {p["name"]: p for p in (await client.get("/api/presets")).json()}
    assert presets["Legacy"]["options"]["tags"] == []
    assert presets["Stale"]["options"]["tags"] == [a["id"]]
