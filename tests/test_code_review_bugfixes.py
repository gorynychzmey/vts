"""Fixes for three nightly code-review findings (vts-cy1, vts-e5l, vts-wpy)."""
from __future__ import annotations

import logging
import uuid

import pytest


# ---------------------------------------------------------------- vts-cy1

def test_non_ascii_bearer_token_does_not_raise():
    """A token with bytes 0x80-0xFF must be hashable, not a 500.

    Starlette decodes header values as latin-1, so
    `Authorization: Bearer vts_\\xff\\xff` reaches hash_token() as a non-ASCII
    str. `.encode("ascii")` raised UnicodeEncodeError, which nothing caught —
    an unauthenticated client could trigger a 500 plus a traceback on any
    authenticated endpoint just by sending junk.
    """
    from vts.services.api_tokens import hash_token

    digest = hash_token("vts_\xff\xfe")
    assert len(digest) == 64
    # Must still be a real hash, not a swallowed error.
    int(digest, 16)


def test_non_ascii_token_hashes_differently_from_its_ascii_neighbour():
    """Encoding must not silently collapse distinct tokens onto one digest."""
    from vts.services.api_tokens import hash_token

    assert hash_token("vts_\xff") != hash_token("vts_")


def test_ascii_tokens_keep_their_existing_digests():
    """Stored hashes were computed from ASCII tokens; changing the encoding
    must not invalidate every token in the database.

    UTF-8 and ASCII agree byte-for-byte on ASCII input, so this holds — but it
    is the one thing that would break silently, so it gets a test.
    """
    import hashlib

    from vts.services.api_tokens import hash_token

    raw = "vts_Bs2a-RZuB1Y5MbBGp36nnBBL"
    assert hash_token(raw) == hashlib.sha256(raw.encode("ascii")).hexdigest()


@pytest.mark.asyncio
async def test_auth_rejects_a_non_ascii_token_with_401(authed_app):
    """End to end through the auth resolver: junk bytes must give 401, not 500.

    Driven at resolve_user_from_request rather than through the test client:
    httpx refuses to encode a non-ASCII header at all, so a client-level test
    would only prove httpx validates. This feeds the resolver exactly what
    Starlette produces from uvicorn's raw bytes (latin-1 decoded).
    """
    from fastapi import HTTPException
    from starlette.requests import Request

    from vts.core.config import get_settings
    from vts.services.auth import resolve_user_from_request

    app, factory = authed_app

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "root_path": "",
        # What Starlette hands the app for raw bytes `Bearer vts_\xff\xff`.
        "headers": [
            (b"host", b"test"),
            (b"authorization", "Bearer vts_\xff\xff".encode("latin-1")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "app": app,
    }
    request = Request(scope)
    # The token really is non-ASCII by the time our code sees it.
    assert request.headers["authorization"].encode("ascii", "ignore") != (
        request.headers["authorization"].encode("latin-1")
    )

    async with factory() as session:
        with pytest.raises(HTTPException) as excinfo:
            await resolve_user_from_request(request, session, get_settings())

    # A clean 401 — before the fix this raised UnicodeEncodeError, which
    # FastAPI turns into a 500 plus a traceback.
    assert excinfo.value.status_code == 401, excinfo.value.status_code


# ---------------------------------------------------------------- vts-e5l

def _processor():
    """A TaskProcessor with just enough state for the logger helpers.

    _task_logger only reads self.settings (for the timezone), so the object is
    built without running __init__ rather than standing up a whole worker.
    """
    from vts.core.config import get_settings
    from vts.pipeline.processor import TaskProcessor

    proc = TaskProcessor.__new__(TaskProcessor)
    proc.settings = get_settings()
    return proc


def test_task_logger_handler_is_closed_and_removed(tmp_path):
    """The per-task FileHandler must not outlive the task.

    logging keeps every named logger for the life of the process, so a worker
    that never closed these accumulated one open fd per task until restart —
    and an unlinked-but-open log file keeps its disk blocks too.
    """
    from vts.pipeline.processor import TaskProcessor

    task_id = uuid.uuid4()
    log_path = tmp_path / "task.log"

    proc = _processor()
    logger = proc._task_logger(task_id=task_id, log_path=log_path)
    handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert handlers, "expected a FileHandler to be attached"

    proc._close_task_logger(task_id=task_id)

    remaining = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert not remaining, f"handler still attached: {remaining}"
    # The stream must actually be closed, not merely detached.
    assert handlers[0].stream is None or handlers[0].stream.closed


def test_closing_the_task_logger_is_safe_to_repeat(tmp_path):
    """process_task's finally block may run for a task whose logger was never
    created (early failure), and must not raise."""
    proc = _processor()

    task_id = uuid.uuid4()
    proc._close_task_logger(task_id=task_id)  # never created

    proc._task_logger(task_id=task_id, log_path=tmp_path / "t.log")
    proc._close_task_logger(task_id=task_id)
    proc._close_task_logger(task_id=task_id)  # twice


def test_repeated_tasks_do_not_accumulate_handlers(tmp_path):
    """The actual leak: N tasks must not leave N open handlers behind."""
    proc = _processor()

    for index in range(25):
        task_id = uuid.uuid4()
        proc._task_logger(task_id=task_id, log_path=tmp_path / f"task-{index}.log")
        proc._close_task_logger(task_id=task_id)

    leaked = [
        name
        for name, logger in logging.Logger.manager.loggerDict.items()
        if name.startswith("task.")
        and isinstance(logger, logging.Logger)
        and any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    ]
    assert not leaked, f"{len(leaked)} task loggers still hold file handlers"


# ---------------------------------------------------------------- vts-wpy

@pytest.mark.asyncio
async def test_donor_lookup_finds_an_older_exact_match(authed_app):
    """A matching donor must be found even when a newer non-matching one exists.

    The query fetched exactly one row — the newest completed task for the URL —
    and only then compared options in Python. So if the same URL had been
    processed twice with different options, the newest row shadowed an older
    exact match and donor-clone silently fell back to full processing, wasting
    a GPU/LLM pass.
    """
    import datetime as dt

    from vts.db.models import Task, TaskStatus, User
    from vts.db.repo import Repo

    _app, factory = authed_app

    wanted = {"language": "en", "transcript": True, "diarize": False}
    other = {"language": "de", "transcript": True, "diarize": True}
    url = "https://example.com/donor-clip.mp4"

    async with factory() as session:
        donor_owner = User(id=uuid.uuid4(), username="donor-owner")
        newer_owner = User(id=uuid.uuid4(), username="newer-owner")
        asker = User(id=uuid.uuid4(), username="asker")
        session.add_all([donor_owner, newer_owner, asker])
        await session.flush()

        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        # OLDER, and the one that actually matches.
        match = Task(
            id=uuid.uuid4(), user_id=donor_owner.id, source_url=url,
            status=TaskStatus.completed, options=dict(wanted),
            artifact_dir="/tmp/donor", updated_at=base,
        )
        # NEWER, different options — this is what used to shadow the match.
        shadow = Task(
            id=uuid.uuid4(), user_id=newer_owner.id, source_url=url,
            status=TaskStatus.completed, options=dict(other),
            artifact_dir="/tmp/shadow", updated_at=base + dt.timedelta(days=1),
        )
        session.add_all([match, shadow])
        await session.commit()

        found = await Repo(session).find_completed_donor(
            source_url=url, options=dict(wanted), exclude_user_id=asker.id
        )

    assert found is not None, "the older exact match was not found"
    assert found.options == wanted


@pytest.mark.asyncio
async def test_donor_lookup_still_returns_nothing_when_options_differ(authed_app):
    """The widened search must not start returning near-misses."""
    import datetime as dt

    from vts.db.models import Task, TaskStatus, User
    from vts.db.repo import Repo

    _app, factory = authed_app
    url = "https://example.com/no-donor.mp4"

    async with factory() as session:
        owner = User(id=uuid.uuid4(), username="owner-nm")
        asker = User(id=uuid.uuid4(), username="asker-nm")
        session.add_all([owner, asker])
        await session.flush()
        session.add(
            Task(
                id=uuid.uuid4(), user_id=owner.id, source_url=url,
                status=TaskStatus.completed,
                options={"language": "de", "transcript": True, "diarize": True},
                artifact_dir="/tmp/nm",
                updated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
        )
        await session.commit()

        found = await Repo(session).find_completed_donor(
            source_url=url,
            options={"language": "en", "transcript": True, "diarize": False},
            exclude_user_id=asker.id,
        )

    assert found is None


@pytest.mark.asyncio
async def test_donor_lookup_excludes_the_asking_user(authed_app):
    """Unchanged behaviour worth pinning while the query is being rewritten."""
    import datetime as dt

    from vts.db.models import Task, TaskStatus, User
    from vts.db.repo import Repo

    _app, factory = authed_app
    url = "https://example.com/own-clip.mp4"
    options = {"language": "en", "transcript": True, "diarize": False}

    async with factory() as session:
        asker = User(id=uuid.uuid4(), username="asker-own")
        session.add(asker)
        await session.flush()
        session.add(
            Task(
                id=uuid.uuid4(), user_id=asker.id, source_url=url,
                status=TaskStatus.completed, options=dict(options),
                artifact_dir="/tmp/own",
                updated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
        )
        await session.commit()

        found = await Repo(session).find_completed_donor(
            source_url=url, options=dict(options), exclude_user_id=asker.id
        )

    assert found is None, "a user's own task must not be its own donor"


@pytest.mark.asyncio
async def test_donor_lookup_prefers_the_newest_among_equal_matches(authed_app):
    """When several donors match, the newest should still win."""
    import datetime as dt

    from vts.db.models import Task, TaskStatus, User
    from vts.db.repo import Repo

    _app, factory = authed_app
    url = "https://example.com/many-donors.mp4"
    options = {"language": "en", "transcript": True, "diarize": False}

    async with factory() as session:
        owner = User(id=uuid.uuid4(), username="owner-many")
        asker = User(id=uuid.uuid4(), username="asker-many")
        session.add_all([owner, asker])
        await session.flush()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        newest_id = uuid.uuid4()
        session.add_all([
            Task(
                id=uuid.uuid4(), user_id=owner.id, source_url=url,
                status=TaskStatus.completed, options=dict(options),
                artifact_dir="/tmp/old", updated_at=base,
            ),
            Task(
                id=newest_id, user_id=owner.id, source_url=url,
                status=TaskStatus.completed, options=dict(options),
                artifact_dir="/tmp/new", updated_at=base + dt.timedelta(days=2),
            ),
        ])
        await session.commit()

        found = await Repo(session).find_completed_donor(
            source_url=url, options=dict(options), exclude_user_id=asker.id
        )

    assert found is not None and found.id == newest_id
