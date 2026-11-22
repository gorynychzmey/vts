"""Content served from the recording; task routes are adapters (vts-lzad).

The recording is the lasting object, so its content endpoints are the real
ones: `/api/recordings/{id}/text/{variant}`, `/entries`, `/subtitles`,
`/results/{source}/{ref}`. The task-addressed routes keep their paths and
responses but resolve the task's recording and serve exactly what the
recording endpoint serves — a twin answers byte for byte the same.

`/player/{id}` takes either a task id or a recording id.
"""
from __future__ import annotations

import json
import uuid

import pytest
from tests.conftest import _TEST_USER_ID

_USER = uuid.UUID(_TEST_USER_ID)

_ENTRIES = [
    {"start": 0.0, "end": 1.5, "text": "Hello there", "speaker": "SPEAKER_00"},
    {"start": 1.5, "end": 3.0, "text": "world", "speaker": "SPEAKER_01"},
]


async def _seed(factory, root, *, user_id=_USER, with_media=False):
    """A task and its recording, every content artifact written and registered
    through the setters (which is how the pipeline records them)."""
    from vts.db.models import User
    from vts.db.repo import Repo

    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "transcript.txt").write_text("raw transcript body " * 20, encoding="utf-8")
    (outputs / "transcript.json").write_text(
        json.dumps({"text": "Hello there world", "entries": _ENTRIES}), encoding="utf-8"
    )
    (outputs / "redacted_transcript.txt").write_text("redacted body " * 20, encoding="utf-8")
    (outputs / "summary.md").write_text("# Summary\n\nshort", encoding="utf-8")
    results = root / "summary" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "user__p1.md").write_text("memo result body", encoding="utf-8")
    if with_media:
        (root / "media").mkdir(exist_ok=True)
        (root / "media" / "audio.original.mp3").write_bytes(b"\x00\x00")

    task_id = uuid.uuid4()
    async with factory() as session:
        if user_id != _USER:
            session.add(User(id=user_id, username=f"other-{user_id.hex[:6]}"))
            await session.flush()
        repo = Repo(session)
        task = await repo.create_task(
            user_id=user_id,
            source_url="https://example.com/v",
            options={"transcript": True, "prompts": [{"source": "user", "id": "p1"}]},
            artifact_dir=str(root),
            task_id=task_id,
        )
        await repo.set_task_content(
            task,
            transcript_path=str(outputs / "transcript.txt"),
            summary_path=str(outputs / "summary.md"),
            redacted_path=str(outputs / "redacted_transcript.txt"),
        )
        await repo.set_task_prompt_results(
            task, [{"source": "user", "id": "p1", "path": str(results / "user__p1.md")}]
        )
        recording = await repo.recording_for_task(task_id)
        recording_id = recording.id
        await session.commit()
    return task_id, recording_id


def _same_response(a, b):
    assert a.status_code == b.status_code
    assert a.content == b.content
    for header in ("content-type", "cache-control", "content-range", "accept-ranges"):
        assert a.headers.get(header) == b.headers.get(header), header


# ------------------------------------------------------------------ twins

_TEXT_TWINS = [
    ("transcript", "raw"),
    ("redacted", "redacted"),
    ("summary", "summary"),
]

_MODES = [
    ({}, {}),
    ({"accept": "application/json"}, {}),
    ({"range": "bytes=2-9"}, {}),
    ({}, {"offset": 3, "limit": 5}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_suffix,variant", _TEXT_TWINS)
@pytest.mark.parametrize("headers,params", _MODES)
async def test_text_twins_answer_the_same(
    authed_app, client, tmp_path, task_suffix, variant, headers, params
):
    _app, factory = authed_app
    task_id, recording_id = await _seed(factory, tmp_path)

    by_task = await client.get(f"/api/tasks/{task_id}/{task_suffix}", headers=headers, params=params)
    by_recording = await client.get(
        f"/api/recordings/{recording_id}/text/{variant}", headers=headers, params=params
    )
    assert by_task.status_code in (200, 206), by_task.text
    _same_response(by_recording, by_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_suffix,recording_suffix", [
    ("transcript-entries", "entries"),
    ("subtitles", "subtitles"),
    ("results/user/p1", "results/user/p1"),
    ("results/system/summary", "results/system/summary"),
])
async def test_derived_twins_answer_the_same(
    authed_app, client, tmp_path, task_suffix, recording_suffix
):
    _app, factory = authed_app
    task_id, recording_id = await _seed(factory, tmp_path)

    by_task = await client.get(f"/api/tasks/{task_id}/{task_suffix}")
    by_recording = await client.get(f"/api/recordings/{recording_id}/{recording_suffix}")
    assert by_task.status_code == 200, by_task.text
    _same_response(by_recording, by_task)


@pytest.mark.asyncio
async def test_recording_entries_carry_the_resolved_blocks(authed_app, client, tmp_path):
    _app, factory = authed_app
    _task_id, recording_id = await _seed(factory, tmp_path)

    r = await client.get(f"/api/recordings/{recording_id}/entries")
    assert r.status_code == 200, r.text
    blocks = r.json()["blocks"]
    assert [b["text"] for b in blocks] == ["Hello there", "world"]
    assert "SPEAKER_00" not in json.dumps([b["label"] for b in blocks])


# ------------------------------------------- task routes read the recording

@pytest.mark.asyncio
async def test_task_routes_serve_the_recordings_content_not_the_tasks(
    authed_app, client, tmp_path
):
    """The task's options carry a stale prompt-result copy on purpose: the
    adapter must follow the recording, which is the owner of the content."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    task_id, _recording_id = await _seed(factory, tmp_path)
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "transcript.txt").write_text("STALE TASK TRANSCRIPT", encoding="utf-8")
    (stale / "summary.md").write_text("STALE TASK SUMMARY", encoding="utf-8")
    (stale / "memo.md").write_text("STALE TASK MEMO", encoding="utf-8")
    async with factory() as session:
        task = await Repo(session).get_task_by_id(task_id)
        task.options = {
            **task.options,
            "prompt_results": [{"source": "user", "id": "p1", "path": str(stale / "memo.md")}],
        }
        await session.commit()

    transcript = await client.get(f"/api/tasks/{task_id}/transcript")
    summary = await client.get(f"/api/tasks/{task_id}/summary")
    memo = await client.get(f"/api/tasks/{task_id}/results/user/p1")
    assert transcript.text.startswith("raw transcript body"), transcript.text
    assert summary.text == "# Summary\n\nshort"
    assert memo.text == "memo result body"


# -------------------------------------------------------------- edge cases

@pytest.mark.asyncio
async def test_task_without_a_recording_is_not_ready_not_a_crash(authed_app, client, tmp_path):
    """Legacy rows from before the backfill, or a race: 404, never 500 — and
    never the task's own copy of the content."""
    _app, factory = authed_app
    from vts.db.models import Recording

    task_id, recording_id = await _seed(factory, tmp_path)
    async with factory() as session:
        await session.delete(await session.get(Recording, recording_id))
        await session.commit()

    for suffix in ("transcript", "summary", "redacted", "results/user/p1"):
        r = await client.get(f"/api/tasks/{task_id}/{suffix}")
        assert r.status_code == 404, (suffix, r.status_code, r.text)
    r = await client.get(f"/api/tasks/{task_id}/transcript")
    assert r.json()["detail"] == "Transcript is not ready"


@pytest.mark.asyncio
async def test_a_missing_file_is_404_not_500(authed_app, client, tmp_path):
    _app, factory = authed_app
    task_id, recording_id = await _seed(factory, tmp_path)
    (tmp_path / "outputs" / "transcript.txt").unlink()
    (tmp_path / "outputs" / "redacted_transcript.txt").unlink()
    (tmp_path / "summary" / "results" / "user__p1.md").unlink()

    for url, detail in (
        (f"/api/recordings/{recording_id}/text/raw", "Transcript file missing"),
        (f"/api/tasks/{task_id}/transcript", "Transcript file missing"),
        (f"/api/recordings/{recording_id}/text/redacted", "Redacted transcript is not ready"),
        (f"/api/recordings/{recording_id}/results/user/p1", "Result not found"),
    ):
        r = await client.get(url)
        assert r.status_code == 404, (url, r.status_code, r.text)
        assert r.json()["detail"] == detail


@pytest.mark.asyncio
async def test_summary_regeneration_hides_the_summary_but_not_the_memos(
    authed_app, client, tmp_path
):
    """During a summary restart `summary_path` is NULL while the old file may
    still sit at the default name. The summary must read as not ready rather
    than serving the stale file; the user prompts' results are unaffected."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    task_id, recording_id = await _seed(factory, tmp_path)
    async with factory() as session:
        repo = Repo(session)
        await repo.set_task_content(await repo.get_task_by_id(task_id), summary_path=None)
        await session.commit()
    assert (tmp_path / "outputs" / "summary.md").exists()

    for url in (
        f"/api/recordings/{recording_id}/text/summary",
        f"/api/tasks/{task_id}/summary",
    ):
        r = await client.get(url)
        assert r.status_code == 404, (url, r.text)
        assert r.json()["detail"] == "Summary is not ready"
    memo = await client.get(f"/api/recordings/{recording_id}/results/user/p1")
    assert memo.status_code == 200 and memo.text == "memo result body"


@pytest.mark.asyncio
async def test_another_users_recording_is_invisible(authed_app, client, tmp_path):
    _app, factory = authed_app
    _task_id, recording_id = await _seed(factory, tmp_path, user_id=uuid.uuid4())

    for suffix in ("text/raw", "entries", "subtitles", "results/user/p1"):
        r = await client.get(f"/api/recordings/{recording_id}/{suffix}")
        assert r.status_code == 404, (suffix, r.status_code)
    assert (await client.get(f"/player/{recording_id}")).status_code == 404


@pytest.mark.asyncio
async def test_unknown_variant_is_rejected(authed_app, client, tmp_path):
    _app, factory = authed_app
    _task_id, recording_id = await _seed(factory, tmp_path)
    r = await client.get(f"/api/recordings/{recording_id}/text/log")
    assert r.status_code == 422


# ------------------------------------------------------------------ player

def _embedded_blocks(html: str) -> list[dict]:
    start = html.index('<script type="application/json" id="vts-player-blocks">')
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    return json.loads(html[start:end])


@pytest.mark.asyncio
async def test_player_by_recording_id_uses_its_tasks_media_and_live_wiring(
    authed_app, client, tmp_path
):
    _app, factory = authed_app
    task_id, recording_id = await _seed(factory, tmp_path, with_media=True)

    r = await client.get(f"/player/{recording_id}")
    assert r.status_code == 200, r.text
    assert [s["text"] for b in _embedded_blocks(r.text) for s in b["sentences"]] == [
        "Hello there", "world",
    ]
    assert f'src="/api/tasks/{task_id}/media"' in r.text
    assert "EventSource" in r.text
    assert json.dumps(str(task_id)) in r.text
    assert str(recording_id) not in r.text


@pytest.mark.asyncio
async def test_player_by_recording_id_without_a_task_shows_the_transcript_only(
    authed_app, client, tmp_path
):
    _app, factory = authed_app
    from vts.db.models import Recording, Task

    task_id, recording_id = await _seed(factory, tmp_path, with_media=True)
    async with factory() as session:
        recording = await session.get(Recording, recording_id)
        recording.source_task_id = None
        await session.delete(await session.get(Task, task_id))
        await session.commit()

    r = await client.get(f"/player/{recording_id}?t=1.5")
    assert r.status_code == 200, r.text
    assert "<video" not in r.text and "<audio" not in r.text
    assert "EventSource" not in r.text
    assert [s["text"] for b in _embedded_blocks(r.text) for s in b["sentences"]] == [
        "Hello there", "world",
    ]
    assert "data-media-unavailable data-transcript-only" in r.text


@pytest.mark.asyncio
async def test_player_by_task_id_with_a_deep_link_is_unchanged(authed_app, client, tmp_path):
    _app, factory = authed_app
    task_id, _recording_id = await _seed(factory, tmp_path, with_media=True)

    r = await client.get(f"/player/{task_id}?t=1.5")
    assert r.status_code == 200, r.text
    assert f'src="/api/tasks/{task_id}/media"' in r.text
    assert "EventSource" in r.text
    assert "<p class=\"media-unavailable\"" not in r.text
    assert len(_embedded_blocks(r.text)) == 2


@pytest.mark.asyncio
async def test_player_for_an_unknown_id_is_404(client):
    assert (await client.get(f"/player/{uuid.uuid4()}")).status_code == 404


# ------------------------------------------------------------- search link

def test_search_hits_link_the_player_by_recording():
    from vts.mcp.tools_registry.search import hit_links

    links = hit_links(
        recording_id="11111111-1111-1111-1111-111111111111",
        start_sec=754.0,
        base_url="https://vts.example",
    )
    assert links["player_url"] == (
        "https://vts.example/player/11111111-1111-1111-1111-111111111111?t=754"
    )


# --------------------------------------------- label language during the run

@pytest.mark.asyncio
async def test_subtitles_label_voices_in_the_chosen_language_before_completion(
    authed_app, client, tmp_path
):
    """A task submitted with an explicit language must label voices in it
    while still running — before any upsert gives the recording its language.
    The task-side reader used the task's options for this."""
    _app, factory = authed_app
    from vts.db.repo import Repo

    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "transcript.json").write_text(
        json.dumps({"text": "Привет", "entries": [
            {"start": 0.0, "end": 1.0, "text": "Привет", "speaker": "SPEAKER_00"},
        ]}),
        encoding="utf-8",
    )
    task_id = uuid.uuid4()
    async with factory() as session:
        await Repo(session).create_task(
            user_id=_USER,
            source_url="https://example.com/v",
            options={"transcript": True, "language": "ru"},
            artifact_dir=str(tmp_path),
            task_id=task_id,
        )
        await session.commit()

    r = await client.get(f"/api/tasks/{task_id}/subtitles")
    assert r.status_code == 200, r.text
    assert "<v Голос 1>" in r.text, r.text


# ------------------------------------------- task cards read the recording

class _FakeRedis:
    """Enough Redis for the queue-position cache the task routes read."""

    async def get(self, key):
        return None

    async def setex(self, key, ttl, value) -> None:
        return None


@pytest.fixture
def wired_redis(authed_app):
    authed_app[0].state.redis = _FakeRedis()


async def _make_task_disagree(factory, task_id, tmp_path):
    """The task's options keep a stale (empty) prompt-result list; the
    recording holds the real content (redacted text and a memo)."""
    from vts.db.repo import Repo

    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "transcript.txt").write_text("STALE", encoding="utf-8")
    (stale / "summary.md").write_text("STALE SUMMARY", encoding="utf-8")
    async with factory() as session:
        task = await Repo(session).get_task_by_id(task_id)
        task.options = {**task.options, "prompt_results": []}
        await session.commit()
    return stale


@pytest.mark.asyncio
async def test_task_card_shows_the_recordings_content_fields(
    authed_app, wired_redis, client, tmp_path
):
    _app, factory = authed_app
    root = tmp_path / "rec"
    task_id, _rid = await _seed(factory, root)
    stale = await _make_task_disagree(factory, task_id, tmp_path)

    card = (await client.get(f"/api/tasks/{task_id}")).json()

    assert card["transcript_path"] == str(root / "outputs" / "transcript.txt")
    assert card["summary_path"] == str(root / "outputs" / "summary.md")
    assert card["redacted_path"] == str(root / "outputs" / "redacted_transcript.txt")
    assert card["options"]["prompt_results"] == [
        {"source": "user", "id": "p1", "path": str(root / "summary" / "results" / "user__p1.md")}
    ]
    assert card["stats"]["transcript_chars"] == len(("raw transcript body " * 20).strip())
    assert card["stats"]["summary_chars"] == len("# Summary\n\nshort")
    assert card["stats"]["redacted_chars"] == len(("redacted body " * 20).strip())
    assert str(stale) not in str(card)


@pytest.mark.asyncio
async def test_serializing_never_mutates_the_tasks_options(authed_app, tmp_path):
    from vts.api._helpers.serialization import serialize_task
    from vts.db.repo import Repo

    _app, factory = authed_app
    task_id, _rid = await _seed(factory, tmp_path / "rec")
    await _make_task_disagree(factory, task_id, tmp_path)
    async with factory() as session:
        repo = Repo(session)
        task = await repo.get_task_by_id(task_id)
        recording = await repo.recording_for_task(task_id)
        before = {k: v for k, v in task.options.items()}
        out = serialize_task(task, recording=recording)
        assert out.options["prompt_results"] != task.options["prompt_results"]
        assert task.options == before
        assert out.options is not task.options


@pytest.mark.asyncio
async def test_task_without_a_recording_shows_no_content(authed_app, tmp_path):
    """The task's own old fields are never served, even when a row still has them."""
    from vts.api._helpers.serialization import serialize_task
    from vts.db.repo import Repo

    _app, factory = authed_app
    task_id, _rid = await _seed(factory, tmp_path / "rec")
    await _make_task_disagree(factory, task_id, tmp_path)
    async with factory() as session:
        task = await Repo(session).get_task_by_id(task_id)
        task.options = {**task.options, "prompt_results": [{"source": "user", "id": "old"}]}
        out = serialize_task(task, recording=None)
        assert (out.transcript_path, out.summary_path, out.redacted_path) == (None, None, None)
        assert out.options["prompt_results"] == []
        assert out.stats.transcript_chars is None and out.stats.summary_chars is None


@pytest.mark.asyncio
async def test_recordings_for_tasks_is_one_query_keyed_by_task(authed_app, tmp_path):
    from vts.db.repo import Repo

    _app, factory = authed_app
    a, a_rec = await _seed(factory, tmp_path / "a")
    b, b_rec = await _seed(factory, tmp_path / "b")
    async with factory() as session:
        found = await Repo(session).recordings_for_tasks([a, b, uuid.uuid4()])
        assert {k: v.id for k, v in found.items()} == {a: a_rec, b: b_rec}
        assert await Repo(session).recordings_for_tasks([]) == {}


@pytest.mark.asyncio
async def test_task_list_loads_recordings_once_per_page(
    authed_app, wired_redis, client, tmp_path
):
    from sqlalchemy import event

    _app, factory = authed_app
    ids = [(await _seed(factory, tmp_path / f"r{i}"))[0] for i in range(3)]
    engine = factory.kw["bind"]
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        if "FROM recordings" in statement and "source_task_id" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        for compact in ("false", "true"):
            statements.clear()
            r = await client.get("/api/tasks", params={"compact": compact})
            assert r.status_code == 200
            assert {t["id"] for t in r.json()} >= {str(i) for i in ids}
            assert len(statements) == 1, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)
