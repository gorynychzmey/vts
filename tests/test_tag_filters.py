"""Filtering tasks and the library by tags (vts-qwcj).

List and count must agree for every combination, so each check goes through
both endpoints of a pair.
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import _TEST_USER_ID
from vts.db.models import User
from vts.db.repo import Repo

pytestmark = pytest.mark.asyncio

USER_ID = uuid.UUID(_TEST_USER_ID)
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f3")


class _FakeRedis:
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


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    authed_app[0].state.redis = _FakeRedis()


@pytest.fixture
def factory(authed_app):
    return authed_app[1]


async def _make(factory, title, tags, *, user_id=USER_ID):
    async with factory() as s:
        repo = Repo(s)
        d = f"/tmp/tag-filters-{uuid.uuid4()}"
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
        s.add(User(id=OTHER_ID, username="other-f"))
        await s.commit()
    a = await _make(factory, "alpha", ["Work", "Home"])
    b = await _make(factory, "beta", ["work"])
    c = await _make(factory, "gamma", [])
    # Another user's tag with the same key must never match ours.
    await _make(factory, "delta", ["work", "home"], user_id=OTHER_ID)
    # A task without any recording at all has no tags.
    async with factory() as s:
        await Repo(s).create_task(
            user_id=USER_ID, source_url="https://example.com/bare",
            options={}, artifact_dir=f"/tmp/tag-filters-{uuid.uuid4()}",
        )
        await s.commit()
    return {"alpha": a, "beta": b, "gamma": c}


def _qs(tags, match=None, extra=""):
    parts = [f"tag={t}" for t in tags]
    if match:
        parts.append(f"tag_match={match}")
    return "&".join(parts) + (("&" + extra) if extra else "")


async def _task_titles(client, qs):
    r = await client.get(f"/api/tasks?{qs}")
    assert r.status_code == 200, r.text
    titles = sorted((t["source_title"] for t in r.json()), key=str)
    c = await client.get(f"/api/tasks/count?{qs}")
    assert c.status_code == 200, c.text
    assert c.json()["total"] == len(titles), (qs, titles)
    return titles


async def _rec_titles(client, qs):
    r = await client.get(f"/api/recordings?{qs}")
    assert r.status_code == 200, r.text
    body = r.json()
    titles = sorted(i["title"] for i in body["items"])
    assert body["total"] == len(titles), (qs, titles)
    return titles


@pytest.mark.parametrize("fn", [_task_titles, _rec_titles])
async def test_all_any_unknown_case(client, corpus, fn):
    assert await fn(client, _qs(["work"])) == ["alpha", "beta"]
    assert await fn(client, _qs(["WORK"])) == ["alpha", "beta"]
    assert await fn(client, _qs(["  work "])) == ["alpha", "beta"]
    # default is all
    assert await fn(client, _qs(["work", "home"])) == ["alpha"]
    assert await fn(client, _qs(["work", "home"], "all")) == ["alpha"]
    assert await fn(client, _qs(["work", "home"], "any")) == ["alpha", "beta"]
    # unknown: all -> empty, any -> ignored
    assert await fn(client, _qs(["work", "nope"], "all")) == []
    assert await fn(client, _qs(["work", "nope"], "any")) == ["alpha", "beta"]
    assert await fn(client, _qs(["nope"], "any")) == []
    # invalid names (empty, too long) count as unknown
    assert await fn(client, _qs(["work", "x" * 65], "all")) == []
    assert await fn(client, _qs(["work", "x" * 65], "any")) == ["alpha", "beta"]
    assert await fn(client, _qs(["x" * 65], "any")) == []
    # duplicates collapse
    assert await fn(client, _qs(["work", "WORK"])) == ["alpha", "beta"]


async def test_other_users_tags_do_not_leak(client, corpus):
    assert await _task_titles(client, _qs(["home"])) == ["alpha"]
    assert await _rec_titles(client, _qs(["home"])) == ["alpha"]


async def test_tag_combines_with_other_filters_and_cursors(client, corpus):
    assert await _task_titles(client, _qs(["work"], extra="q=alp")) == ["alpha"]
    assert await _rec_titles(client, _qs(["work"], extra="q=bet")) == ["beta"]
    assert await _task_titles(client, _qs(["work"], extra="created_from=2999-01-01T00:00:00Z")) == []
    # Cursor paging of the filtered task list: no row skipped or repeated.
    seen: list[str] = []
    params: dict = {"tag": ["work"], "limit": 1}
    for _ in range(4):
        r = (await client.get("/api/tasks", params=params)).json()
        if not r:
            break
        seen.append(r[0]["source_title"])
        params = {
            "tag": ["work"], "limit": 1,
            "before_ts": r[0]["created_at"], "before_id": r[0]["id"],
        }
    assert sorted(seen) == ["alpha", "beta"]


@pytest.mark.parametrize("path", ["/api/tasks", "/api/tasks/count", "/api/recordings"])
async def test_bad_tag_match_is_422(client, path):
    r = await client.get(f"{path}?tag=x&tag_match=some")
    assert r.status_code == 422


@pytest.mark.parametrize("fn", [_task_titles, _rec_titles])
@pytest.mark.parametrize("blank", ["", "%20%20"])
async def test_blank_tag_filters_like_no_tag(client, corpus, fn, blank):
    everything = await fn(client, "")
    assert len(everything) >= 3
    assert await fn(client, f"tag={blank}") == everything
    assert await fn(client, f"tag={blank}&tag_match=any") == everything
    # A blank entry beside a real one drops out; the real one still filters.
    assert await fn(client, f"tag={blank}&tag=work") == ["alpha", "beta"]
