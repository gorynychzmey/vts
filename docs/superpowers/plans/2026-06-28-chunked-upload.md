# Chunked Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users upload large media files (>proxy limit, target 1 GB+) via the web UI by splitting the body into small chunks that each stay under the fronting-proxy limit, with resumable progress; the existing small-file flow is untouched.

**Architecture:** A pure-ish `UploadSession` service (read/write `upload.json` sidecar, append chunks to `<task_dir>/media/audio.original<suffix>.part`, validate offset, finalize via atomic rename) is driven by 5 thin FastAPI endpoints under `/api/uploads`. Task DB row is created only on finalize, reusing the existing enqueue tail (extracted to `_enqueue_uploaded_task`). The client branches in `createTask`: files ≤ a server-configured threshold use the current single-shot POST; larger files use a new chunked loop with a determinate progress ring.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, pytest (+ Postgres fixture). Client: vanilla JS (`vts/static/app.js`). No new packages, no object store, no DB table.

## Global Constraints

- Self-hosted / on-prem: no external services, no new dependencies, local disk only (project CLAUDE.md).
- `upload_id == task_id` (same uuid). `task_dir` created on init; the DB Task row is created ONLY on finalize.
- Staging file is `<task_dir>/media/audio.original<suffix>.part`; finalize renames it (same volume, atomic) to `audio.original<suffix>`. **`<suffix>` INCLUDES the leading dot** (`Path(name).suffix.lower()` → `.mp4`), so the final name is `audio.original.mp4` — found by the pipeline glob `audio.original.*`. `audio.original` is the existing convention for the source media regardless of video/audio.
- Auth: every `/api/uploads/*` endpoint scopes by effective `uuid.UUID(user.id)` (impersonation works like `/api/prompts`); a session whose `user_id` ≠ current → 404 (don't reveal existence). Never build a path from the user's `filename` (use only its `suffix` and `Path(name).name`).
- Suffix + options validated on init (against `_ALLOWED_UPLOAD_SUFFIXES`, the same frozenset as `upload_task`, which must be lifted to module level). `prompts` require `transcript`.
- PATCH `?offset=N` must equal the current `.part` size, else 409. Overflow (`current + chunk > total_size`) → 413. `total_size <= 0` or `> max_upload_bytes` → 413/422 on init.
- Config (env prefix `VTS_`, on `Settings` in `vts/core/config.py`): `upload_chunked_threshold_bytes=52_428_800`, `upload_chunk_bytes=8_388_608`, `max_upload_bytes=2_147_483_648`, `upload_session_ttl_seconds=86_400` (last one stored now for the GC followup vts-ee3; unused here).
- Client progress ring is determinate from the start of a chunked upload: `setProgress(received / total_size)` after each PATCH, using the server-confirmed `received`. Not the indefinite spinner.
- Bump `vts/__init__.py __version__` once, in the client task (Task 4).
- Python interpreter: `/home/victor/dev/vts/.venv/bin/python`. Postgres-backed API tests set `VTS_ARTIFACTS_ROOT` to a tmp dir via monkeypatch (the autouse `_isolate_settings_per_test` fixture clears the settings cache so it takes effect). Test user is `tester`, id `00000000-0000-0000-0000-0000000000a1`.
- Out of scope: background GC of abandoned sessions (vts-ee3), parallel chunks, object store.

---

### Task 1: `UploadSession` service (pure, tmp-dir tested)

**Files:**
- Create: `vts/services/upload_session.py`
- Test: `tests/test_upload_session.py`

**Interfaces:**
- Produces (relied on by Task 3):
  - `class UploadSession` with classmethods/methods operating on a base `artifacts_root: Path` and `username: str`.
  - `UploadSession.init(artifacts_root, username, *, user_id: str, upload_id: uuid.UUID, suffix: str, total_size: int, options: dict, display_name: str | None, filename: str, created_at: str) -> Path` — creates `task_dir/media`, writes the empty `.part`, writes `upload.json` (including the original `filename` and `display_name`); returns the task_dir.
  - `UploadSession.load(artifacts_root, username, upload_id) -> dict | None` — reads `upload.json` (None if missing).
  - `UploadSession.part_path(artifacts_root, username, upload_id, suffix) -> Path`.
  - `UploadSession.received_bytes(part_path) -> int` — current `.part` size on disk (0 if absent).
  - `UploadSession.append_chunk(part_path, meta_path, data: bytes, total_size: int) -> int` — appends, updates `received` in `upload.json`, returns new size. (Caller checks offset/overflow first.)
  - `UploadSession.finalize(part_path, suffix, meta_path) -> Path` — atomic rename `.part`→final, deletes `upload.json`, returns final media path.

Keep all path math here; no FastAPI imports. The methods take explicit paths so they unit-test on a `tmp_path` with zero HTTP/DB.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upload_session.py
from __future__ import annotations

import json
import uuid

from vts.services.upload_session import UploadSession


def test_init_creates_structure_and_sidecar(tmp_path):
    uid = uuid.uuid4()
    task_dir = UploadSession.init(
        tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mp4",
        total_size=100, options={"transcript": True}, display_name="Clip",
        filename="movie.mp4", created_at="2026-06-28T00:00:00Z",
    )
    part = task_dir / "media" / "audio.original.mp4.part"
    meta = task_dir / "upload.json"
    assert part.exists() and part.stat().st_size == 0
    data = json.loads(meta.read_text())
    assert data["user_id"] == "u1"
    assert data["suffix"] == ".mp4"
    assert data["total_size"] == 100
    assert data["received"] == 0
    assert data["display_name"] == "Clip"
    assert data["filename"] == "movie.mp4"


def test_append_grows_part_and_received(tmp_path):
    uid = uuid.uuid4()
    UploadSession.init(tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mp4",
                       total_size=6, options={}, display_name=None, filename="a.mp4", created_at="t")
    part = UploadSession.part_path(tmp_path, "tester", uid, ".mp4")
    meta = part.parent.parent / "upload.json"
    assert UploadSession.received_bytes(part) == 0
    n = UploadSession.append_chunk(part, meta, b"abc", total_size=6)
    assert n == 3
    n = UploadSession.append_chunk(part, meta, b"def", total_size=6)
    assert n == 6
    assert part.read_bytes() == b"abcdef"
    assert json.loads(meta.read_text())["received"] == 6


def test_finalize_renames_and_clears_sidecar(tmp_path):
    uid = uuid.uuid4()
    UploadSession.init(tmp_path, "tester", user_id="u1", upload_id=uid, suffix=".mkv",
                       total_size=3, options={}, display_name=None, filename="b.mkv", created_at="t")
    part = UploadSession.part_path(tmp_path, "tester", uid, ".mkv")
    meta = part.parent.parent / "upload.json"
    UploadSession.append_chunk(part, meta, b"xyz", total_size=3)
    final = UploadSession.finalize(part, ".mkv", meta)
    assert final.name == "audio.original.mkv"
    assert final.read_bytes() == b"xyz"
    assert not part.exists()
    assert not meta.exists()


def test_load_returns_none_when_absent(tmp_path):
    assert UploadSession.load(tmp_path, "tester", uuid.uuid4()) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_upload_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.services.upload_session'`.

- [ ] **Step 3: Implement**

```python
# vts/services/upload_session.py
"""Chunked-upload session storage (vts-b8j).

State lives entirely on local disk under artifacts_root:
  <task_dir>/upload.json                    -- session metadata sidecar
  <task_dir>/media/audio.original<suffix>.part  -- staging file (chunks appended)

No DB row exists until finalize. Pure path/file logic — no FastAPI/DB imports —
so it unit-tests on a tmp dir. task_dir layout matches vts.services.storage.task_dir.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from vts.services.storage import task_dir


def _media_name(suffix: str) -> str:
    # suffix includes the leading dot; result e.g. audio.original.mp4
    return f"audio.original{suffix}"


class UploadSession:
    @staticmethod
    def _dir(artifacts_root: Path, username: str, upload_id: uuid.UUID) -> Path:
        return task_dir(artifacts_root, username, upload_id)

    @classmethod
    def part_path(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID, suffix: str) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "media" / f"{_media_name(suffix)}.part"

    @classmethod
    def meta_path(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "upload.json"

    @classmethod
    def init(
        cls,
        artifacts_root: Path,
        username: str,
        *,
        user_id: str,
        upload_id: uuid.UUID,
        suffix: str,
        total_size: int,
        options: dict,
        display_name: str | None,
        filename: str,
        created_at: str,
    ) -> Path:
        d = cls._dir(artifacts_root, username, upload_id)
        media = d / "media"
        media.mkdir(parents=True, exist_ok=True)
        part = media / f"{_media_name(suffix)}.part"
        part.touch(exist_ok=True)
        meta = {
            "upload_id": str(upload_id),
            "user_id": user_id,
            "username": username,
            "suffix": suffix,
            "total_size": total_size,
            "received": 0,
            "options": options,
            "display_name": display_name,
            "filename": filename,
            "created_at": created_at,
        }
        cls.meta_path(artifacts_root, username, upload_id).write_text(
            json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8"
        )
        return d

    @classmethod
    def load(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID) -> dict | None:
        p = cls.meta_path(artifacts_root, username, upload_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    @staticmethod
    def received_bytes(part_path: Path) -> int:
        return part_path.stat().st_size if part_path.exists() else 0

    @staticmethod
    def append_chunk(part_path: Path, meta_path: Path, data: bytes, total_size: int) -> int:
        with open(part_path, "ab") as f:
            f.write(data)
        new_size = part_path.stat().st_size
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["received"] = new_size
            meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass
        return new_size

    @staticmethod
    def finalize(part_path: Path, suffix: str, meta_path: Path) -> Path:
        final = part_path.with_name(_media_name(suffix))
        part_path.rename(final)  # same dir/volume -> atomic
        try:
            meta_path.unlink()
        except OSError:
            pass
        return final
```

- [ ] **Step 4: Run to verify pass**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_upload_session.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add vts/services/upload_session.py tests/test_upload_session.py
git commit -m "feat(upload): UploadSession chunked-staging service (vts-b8j)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Config settings + lift shared upload helpers

**Files:**
- Modify: `vts/core/config.py` (4 settings)
- Modify: `vts/api/main.py` (lift `_ALLOWED_UPLOAD_SUFFIXES` to module level; extract `_enqueue_uploaded_task` and `_normalize_prompts_json` helpers)
- Test: `tests/test_upload_config_and_helpers.py`

**Interfaces:**
- Produces (relied on by Tasks 3, 4):
  - Settings: `upload_chunked_threshold_bytes`, `upload_chunk_bytes`, `max_upload_bytes`, `upload_session_ttl_seconds`.
  - Module-level `_ALLOWED_UPLOAD_SUFFIXES: frozenset[str]` in `vts/api/main.py`.
  - `def _normalize_prompts_json(prompts: str | None) -> list[dict]` — the prompts-parsing block currently inline in `upload_task`, raising `HTTPException(422,...)` on bad input. (Pure-ish; used by init endpoint.)
  - `async def _enqueue_uploaded_task(task, repo, redis, settings) -> "TaskOut"` — the post-create tail of `upload_task` (notify_queued + publish_event + serialize_task). Returns the serialized task.

**Why one task:** these are the DRY extractions both later tasks depend on; lifting them is mechanical and best reviewed together with the new settings.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upload_config_and_helpers.py
import os
import pytest


def test_upload_settings_defaults(monkeypatch):
    monkeypatch.setenv("VTS_DATABASE_URL", "postgresql+asyncpg://x/y")
    import vts.core.config as c
    c.get_settings.cache_clear()
    s = c.get_settings()
    assert s.upload_chunked_threshold_bytes == 52_428_800
    assert s.upload_chunk_bytes == 8_388_608
    assert s.max_upload_bytes == 2_147_483_648
    assert s.upload_session_ttl_seconds == 86_400
    c.get_settings.cache_clear()


def test_allowed_suffixes_module_level():
    from vts.api.main import _ALLOWED_UPLOAD_SUFFIXES
    assert ".mp4" in _ALLOWED_UPLOAD_SUFFIXES
    assert ".m4a" in _ALLOWED_UPLOAD_SUFFIXES


def test_normalize_prompts_json_default_and_parse():
    from vts.api.main import _normalize_prompts_json
    assert _normalize_prompts_json(None) == [{"source": "system", "id": "summary"}]
    out = _normalize_prompts_json('[{"source":"system","id":"summary"}]')
    assert out == [{"source": "system", "id": "summary"}]


def test_normalize_prompts_json_rejects_bad():
    from fastapi import HTTPException
    from vts.api.main import _normalize_prompts_json
    with pytest.raises(HTTPException):
        _normalize_prompts_json("{not json")
    with pytest.raises(HTTPException):
        _normalize_prompts_json('{"not":"a list"}')
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_upload_config_and_helpers.py -v`
Expected: FAIL — settings attrs missing / `_normalize_prompts_json` not importable.

- [ ] **Step 3a: Add settings** (`vts/core/config.py`, near other feature toggles)

```python
    upload_chunked_threshold_bytes: int = 52_428_800
    upload_chunk_bytes: int = 8_388_608
    max_upload_bytes: int = 2_147_483_648
    upload_session_ttl_seconds: int = 86_400
```

- [ ] **Step 3b: Lift `_ALLOWED_UPLOAD_SUFFIXES` to module level** in `vts/api/main.py`. Move the `frozenset({...})` (currently defined inside `create_app`, above `upload_task`) to module scope (e.g. just below the imports). Update `upload_task` to reference the module-level name (delete its local definition). Keep the exact same suffix set.

- [ ] **Step 3c: Extract `_normalize_prompts_json`** at module level in `vts/api/main.py`, lifting the prompts-parsing block from `upload_task`:

```python
def _normalize_prompts_json(prompts: str | None) -> list[dict]:
    from vts.services.prompt_registry import parse_ref, ref_to_dict
    if prompts is None:
        return [{"source": "system", "id": "summary"}]
    try:
        raw_refs = json.loads(prompts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="prompts must be valid JSON") from exc
    if not isinstance(raw_refs, list):
        raise HTTPException(status_code=422, detail="prompts must be a JSON list")
    out: list[dict] = []
    for entry in raw_refs:
        try:
            source, ref_id = parse_ref(entry)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid prompt ref: {entry!r}") from exc
        out.append(ref_to_dict(source, ref_id))
    return out
```
Then replace that block inside `upload_task` with `normalized_prompts = _normalize_prompts_json(prompts)` (keep the subsequent `if normalized_prompts and not transcript: raise 422` check in `upload_task`).

- [ ] **Step 3d: Extract `_enqueue_uploaded_task`** at module level in `vts/api/main.py`, lifting the post-`create_task` tail of `upload_task`:

```python
async def _enqueue_uploaded_task(task, repo, redis, settings) -> "TaskOut":
    bus = RedisBus(redis, settings)
    await bus.notify_queued()
    await bus.publish_event(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event="task_status",
        data={"status": task.status.value},
    )
    set_committed_value(task, "steps", [])
    queue_positions = await _get_cached_queue_positions(redis, repo, settings.redis_prefix)
    asr_progress = await repo.get_asr_progress_for_tasks([task.id])
    summary_progress = {task.id: summary_progress_for_task(task)}
    return serialize_task(task, queue_positions, asr_progress, summary_progress)
```
Replace the equivalent tail in `upload_task` (after `await session.commit()`) with `return await _enqueue_uploaded_task(task, repo, redis, settings)`. Behavior must be byte-for-byte identical for the existing single-shot path.

- [ ] **Step 4: Run to verify pass + no regression on existing upload behavior**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_upload_config_and_helpers.py tests/test_upload_display_name.py -v`
Expected: PASS. Then `node --check` is N/A. Confirm app imports: `/home/victor/dev/vts/.venv/bin/python -c "import vts.api.main; print('ok')"` → `ok`.

- [ ] **Step 5: Commit**

```bash
git add vts/core/config.py vts/api/main.py tests/test_upload_config_and_helpers.py
git commit -m "refactor(upload): lift suffix set + enqueue/prompts helpers; add chunked-upload settings (vts-b8j)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The 5 `/api/uploads` endpoints

**Files:**
- Modify: `vts/api/main.py` (add 5 endpoints inside `create_app`, near `upload_task`)
- Test: `tests/test_uploads_api.py` (Postgres + httpx + tmp artifacts_root)

**Interfaces:**
- Consumes: `UploadSession` (Task 1); `_ALLOWED_UPLOAD_SUFFIXES`, `_normalize_prompts_json`, `_enqueue_uploaded_task`, `normalize_display_name` (Task 2 + existing); `Repo.create_task`; `task_dir`.
- Produces: `GET /api/uploads/config`, `POST /api/uploads/init`, `GET /api/uploads/{upload_id}/offset`, `PATCH /api/uploads/{upload_id}`, `POST /api/uploads/{upload_id}/finalize`.

Pydantic request/response models (add to `vts/api/schemas.py`):
- `UploadConfigOut(chunked_threshold_bytes: int, chunk_bytes: int, max_upload_bytes: int)`
- `UploadInitRequest(filename: str, total_size: int, language: str | None = None, audio_only: bool = False, transcript: bool = True, prompts: str | None = None, display_name: str | None = None)`
- `UploadInitOut(upload_id: str, chunk_size: int)`
- `UploadOffsetOut(received: int, total_size: int)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_uploads_api.py
from __future__ import annotations

import uuid
import pytest

pytestmark = pytest.mark.asyncio

_INIT = {"filename": "clip.mp4", "total_size": 6, "transcript": True}


async def _init(client):
    r = await client.post("/api/uploads/init", json=_INIT)
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


async def test_config_returns_thresholds(client):
    body = (await client.get("/api/uploads/config")).json()
    assert body["chunked_threshold_bytes"] == 52_428_800
    assert body["chunk_bytes"] == 8_388_608
    assert body["max_upload_bytes"] == 2_147_483_648


async def test_happy_path_creates_queued_task(client):
    uid = await _init(client)
    r1 = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"abc")
    assert r1.status_code == 200 and r1.json()["received"] == 3
    r2 = await client.patch(f"/api/uploads/{uid}?offset=3", content=b"def")
    assert r2.json()["received"] == 6
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text
    task = fin.json()
    assert task["status"] == "queued"
    # task is listed now
    tasks = (await client.get("/api/tasks")).json()
    assert any(t["id"] == task["id"] for t in tasks)


async def test_offset_endpoint_supports_resume(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"ab")
    off = (await client.get(f"/api/uploads/{uid}/offset")).json()
    assert off == {"received": 2, "total_size": 6}
    await client.patch(f"/api/uploads/{uid}?offset=2", content=b"cdef")
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200


async def test_wrong_offset_conflicts(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"abc")
    bad = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"x")
    assert bad.status_code == 409


async def test_overflow_rejected(client):
    uid = await _init(client)
    over = await client.patch(f"/api/uploads/{uid}?offset=0", content=b"toolong!")
    assert over.status_code == 413


async def test_init_rejects_bad_suffix(client):
    r = await client.post("/api/uploads/init", json={"filename": "x.txt", "total_size": 5})
    assert r.status_code == 422


async def test_init_rejects_oversize(client):
    r = await client.post("/api/uploads/init",
                          json={"filename": "x.mp4", "total_size": 2_147_483_649})
    assert r.status_code in (413, 422)


async def test_finalize_incomplete_conflicts(client):
    uid = await _init(client)
    await client.patch(f"/api/uploads/{uid}?offset=0", content=b"ab")
    fin = await client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 409


async def test_unknown_upload_is_404(client):
    r = await client.get(f"/api/uploads/{uuid.uuid4()}/offset")
    assert r.status_code == 404
```

**Test fixture note:** these run on the `client` fixture but need `artifacts_root` on a writable tmp dir. Add a module-autouse fixture at the top of the file that sets it BEFORE the app builds:
```python
import pytest

@pytest.fixture(autouse=True)
def _tmp_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    # _isolate_settings_per_test (autouse in conftest) clears the settings
    # cache around each test, so the env var is picked up by create_app().
    yield
```
This must be declared so it runs before the `client`/`authed_app` fixtures build the app. If ordering is a problem, set the env at import time of the test module instead (`os.environ.setdefault`), but prefer the fixture.

- [ ] **Step 2: Run to verify failure**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_uploads_api.py -v`
Expected: FAIL — 404 on `/api/uploads/config` (endpoints not defined).

- [ ] **Step 3a: Add schemas** to `vts/api/schemas.py` (the 4 models listed in Interfaces).

- [ ] **Step 3b: Add the endpoints** inside `create_app` in `vts/api/main.py`, after `upload_task`. Import the new schemas and `UploadSession`, `datetime`/`timezone` for `created_at`.

```python
    @app.get("/api/uploads/config", response_model=UploadConfigOut)
    async def uploads_config(settings: Settings = Depends(get_settings_dep)) -> UploadConfigOut:
        return UploadConfigOut(
            chunked_threshold_bytes=settings.upload_chunked_threshold_bytes,
            chunk_bytes=settings.upload_chunk_bytes,
            max_upload_bytes=settings.max_upload_bytes,
        )

    @app.post("/api/uploads/init", response_model=UploadInitOut)
    async def uploads_init(
        payload: UploadInitRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> UploadInitOut:
        suffix = Path(payload.filename).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix or '(none)'}")
        if payload.total_size <= 0:
            raise HTTPException(status_code=422, detail="total_size must be positive")
        if payload.total_size > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="File exceeds maximum upload size")
        normalized_prompts = _normalize_prompts_json(payload.prompts)
        if normalized_prompts and not payload.transcript:
            raise HTTPException(status_code=422, detail="prompts require transcript")
        upload_id = uuid.uuid4()
        options = {
            "language": payload.language or None,
            "audio_only": payload.audio_only,
            "transcript": payload.transcript,
            "prompts": normalized_prompts,
        }
        UploadSession.init(
            settings.artifacts_root, user.username,
            user_id=user.id, upload_id=upload_id, suffix=suffix,
            total_size=payload.total_size, options=options,
            display_name=normalize_display_name(payload.display_name),
            filename=payload.filename,
            created_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        return UploadInitOut(upload_id=str(upload_id), chunk_size=settings.upload_chunk_bytes)

    def _load_owned_session(settings, user, upload_id_str: str):
        try:
            upload_id = uuid.UUID(upload_id_str)
        except ValueError:
            raise HTTPException(status_code=404, detail="Upload not found")
        meta = UploadSession.load(settings.artifacts_root, user.username, upload_id)
        if meta is None or meta.get("user_id") != user.id:
            raise HTTPException(status_code=404, detail="Upload not found")
        return upload_id, meta

    @app.get("/api/uploads/{upload_id}/offset", response_model=UploadOffsetOut)
    async def uploads_offset(
        upload_id: str,
        user: AuthenticatedUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> UploadOffsetOut:
        uid, meta = _load_owned_session(settings, user, upload_id)
        part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
        return UploadOffsetOut(received=UploadSession.received_bytes(part), total_size=meta["total_size"])

    @app.patch("/api/uploads/{upload_id}")
    async def uploads_patch(
        upload_id: str,
        request: Request,
        offset: int,
        user: AuthenticatedUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> JSONResponse:
        uid, meta = _load_owned_session(settings, user, upload_id)
        part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
        current = UploadSession.received_bytes(part)
        if offset != current:
            raise HTTPException(status_code=409, detail=f"Offset mismatch; expected {current}")
        data = await request.body()
        if current + len(data) > meta["total_size"]:
            raise HTTPException(status_code=413, detail="Chunk exceeds declared total_size")
        meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
        new_size = await asyncio.to_thread(
            UploadSession.append_chunk, part, meta_path, data, meta["total_size"]
        )
        return JSONResponse({"received": new_size})

    @app.post("/api/uploads/{upload_id}/finalize", response_model=TaskOut)
    async def uploads_finalize(
        upload_id: str,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> TaskOut:
        uid, meta = _load_owned_session(settings, user, upload_id)
        part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
        if UploadSession.received_bytes(part) != meta["total_size"]:
            raise HTTPException(status_code=409, detail="Upload incomplete")
        meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
        await asyncio.to_thread(UploadSession.finalize, part, meta["suffix"], meta_path)
        repo = Repo(session)
        artifact = task_dir(settings.artifacts_root, user.username, uid)
        source_url = f"file://{Path(meta['filename']).name}"
        task = await repo.create_task(
            user_id=uuid.UUID(user.id),
            source_url=source_url,
            options=meta["options"],
            artifact_dir=str(artifact),
            task_id=uid,
            source_title=meta.get("display_name"),
        )
        await session.commit()
        return await _enqueue_uploaded_task(task, repo, redis, settings)
```

The original `filename` comes from the sidecar (`meta["filename"]`, stored by `UploadSession.init` in Task 1), so `source_url` matches `upload_task`'s `file://<name>` form. The path itself is built only from the uuid `uid` and the validated `suffix` — never from `meta["filename"]` — so a malicious filename can't escape the task_dir.

- [ ] **Step 4: Run to verify pass**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_uploads_api.py tests/test_upload_session.py -v`
Expected: all PASS (9 api + 4 session).

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py vts/api/schemas.py vts/services/upload_session.py tests/test_uploads_api.py tests/test_upload_session.py
git commit -m "feat(api): /api/uploads chunked endpoints (init/offset/patch/finalize/config) (vts-b8j)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Client chunked upload + determinate progress + version bump

**Files:**
- Modify: `vts/static/app.js` (add `loadUploadConfig`, `uploadFileChunked`; branch in `createTask`)
- Modify: `vts/__init__.py` (version bump)

**Interfaces:**
- Consumes: `GET /api/uploads/config`, `POST /api/uploads/init`, `PATCH /api/uploads/{id}?offset=`, `GET /api/uploads/{id}/offset`, `POST /api/uploads/{id}/finalize`.

- [ ] **Step 1: Add `uploadConfig` state + loader** near the other module state and bootstrap. After the constants block:
```javascript
let uploadConfig = null;
async function loadUploadConfig() {
  try {
    uploadConfig = await api("/api/uploads/config");
  } catch {
    uploadConfig = null; // fall back to single-shot for all sizes
  }
}
```
Call it in `bootstrap()` after `loadProgressWeights()`:
```javascript
  await loadProgressWeights();
  await loadUploadConfig();
```

- [ ] **Step 2: Add `uploadFileChunked`** near `uploadFileWithProgress`. It reuses the same progress ring, determinate from the start:
```javascript
async function uploadFileChunked(file, fields) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;
  const setProgress = (r) => { if (fill) fill.style.strokeDashoffset = circumference * (1 - r); };

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  setProgress(0); // determinate from the start, not the indefinite spinner

  try {
    const init = await api("/api/uploads/init", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        total_size: file.size,
        language: fields.language || null,
        audio_only: fields.audio_only,
        transcript: fields.transcript,
        prompts: fields.prompts,            // already a JSON string
        display_name: fields.display_name || null,
      }),
    });
    const uploadId = init.upload_id;
    const chunkSize = init.chunk_size || 8388608;
    let offset = 0;
    while (offset < file.size) {
      const slice = file.slice(offset, Math.min(offset + chunkSize, file.size));
      const buf = await slice.arrayBuffer();
      let resp;
      try {
        resp = await api(`/api/uploads/${uploadId}?offset=${offset}`, {
          method: "PATCH",
          body: buf,
          headers: { "Content-Type": "application/offset+octet-stream" },
          raw: true, // see api() note below
        });
      } catch (err) {
        // On offset conflict or transient error, re-sync from the server.
        const off = await api(`/api/uploads/${uploadId}/offset`);
        offset = off.received;
        setProgress(offset / file.size);
        continue;
      }
      offset = resp.received;
      setProgress(offset / file.size);
    }
    await api(`/api/uploads/${uploadId}/finalize`, { method: "POST" });
    setProgress(1);
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
  }
}
```
**`api()` integration note for the implementer:** check the existing `api()` helper's signature in `app.js` (search `function api(` / `async function api(`). The pseudo-options above (`method`, `body`, `headers`, `raw`) must match how `api()` actually works. If `api()` always JSON-parses and sets JSON content-type, you cannot send a raw binary PATCH through it — in that case use a direct `fetch(buildPath(...), {...})` for the PATCH (binary body) and the JSON endpoints can stay on `api()`. Use `buildPath()` and the same auth header strategy `uploadFileWithProgress` uses (`X-Forwarded-User: state.authUser`). Do NOT invent an `api()` option that doesn't exist — read it first and adapt.

- [ ] **Step 3: Branch in `createTask`** where it currently calls `uploadFileWithProgress(fd)`:
```javascript
  if (isFile && fileInput) {
    const file = fileInput.files[0];
    const fields = {
      language: form.language.value || "",
      audio_only: form.audio_only.checked,
      transcript: form.transcript.checked,
      prompts: JSON.stringify(getSelectedPrompts()),
      display_name: /* same display_name source the form uses today, or "" */ "",
    };
    const threshold = uploadConfig && Number.isFinite(uploadConfig.chunked_threshold_bytes)
      ? uploadConfig.chunked_threshold_bytes
      : Infinity; // no config -> always single-shot (unchanged behavior)
    if (file.size > threshold) {
      await uploadFileChunked(file, fields);
    } else {
      const fd = new FormData();
      fd.append("file", file);
      if (fields.language) fd.append("language", fields.language);
      fd.append("audio_only", fields.audio_only ? "true" : "false");
      fd.append("transcript", fields.transcript ? "true" : "false");
      fd.append("prompts", fields.prompts);
      await uploadFileWithProgress(fd);
    }
    // ...keep whatever post-upload refresh/reset the function already does...
  }
```
**Note:** preserve the existing post-upload steps (form reset, `refreshAll`, etc.) exactly as they are after the current `await uploadFileWithProgress(fd)` line — only the branch above is new. If the form sends `display_name` today, wire it through `fields.display_name`; if it doesn't, leave it `""`.

- [ ] **Step 4: Bump version** — `vts/__init__.py`: `1.1.14` → `1.1.15`.

- [ ] **Step 5: Verify**

Run: `node --check vts/static/app.js && echo "JS OK"`
Expected: `JS OK`.

- [ ] **Step 6: Commit**

```bash
git add vts/static/app.js vts/__init__.py
git commit -m "feat(ui): chunked upload for files over threshold + determinate progress (vts-b8j)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: UI verifier scenario + full gate + close

**Files:**
- Create: `tests/ui/scenarios/chunked-upload.mjs`
- Reference: `tests/ui/harness.mjs`, `tests/ui/scenarios/smoke-boot.mjs`.

**Interfaces:** Consumes the running static frontend via the verifier harness; stubs `/api/uploads/config`.

- [ ] **Step 1: Write the scenario** — boot with `/api/uploads/config` stubbed; assert the app boots (`#app-version` visible, no console errors), proving `loadUploadConfig()` doesn't break bootstrap. (Driving a real >50 MB File through chunking in headless Chromium is disproportionate; the threshold-branch + chunk loop are covered by the JS logic and the API tests. Assert boot-safety here.)

```javascript
// tests/ui/scenarios/chunked-upload.mjs
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "chunked-upload";

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/uploads/config": {
      chunked_threshold_bytes: 52428800,
      chunk_bytes: 8388608,
      max_upload_bytes: 2147483648,
    },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (!(await isVisible(page, "#app-version"))) {
      failures.push("app did not boot (version label missing) with uploads/config stubbed");
    }
    if (errors.length) failures.push(`console errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```
(Confirm `#app-version` is the right boot selector by checking `smoke-boot.mjs`; reuse whatever it asserts.)

- [ ] **Step 2: Run the verifier**

Run: `cd /home/victor/dev/vts/tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`, exit 0, with `chunked-upload` among PASS lines.

- [ ] **Step 3: Run the full feature Python suite**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest tests/test_upload_session.py tests/test_upload_config_and_helpers.py tests/test_uploads_api.py tests/test_upload_display_name.py -v`
Expected: all PASS.

- [ ] **Step 4: Close + push**

```bash
cd /home/victor/dev/vts
bd close vts-b8j --reason="Chunked upload: /api/uploads init/offset/patch/finalize/config + UploadSession local-disk staging (.part -> atomic rename), client threshold-branch (>50MB) with determinate progress, single-shot path unchanged. Tests + UI verifier green. GC of abandoned sessions tracked in vts-ee3."
bd dolt push
git push
git status   # must show up to date with origin
```

---

## Self-Review

**Spec coverage:**
- TUS-style chunked, local disk, minimal protocol (init/patch/finalize + offset) → Tasks 1, 3. ✓
- Threshold 50 MB, server-configured, single-shot unchanged → Task 2 (config), Task 4 (client branch), Task 3 (config endpoint). ✓
- Staging `.part` in task_dir, sidecar `upload.json`, Task row only on finalize → Task 1 + Task 3 finalize. ✓
- `audio.original<suffix>` with leading-dot suffix (video too) → Task 1 `_media_name` + global constraint. ✓
- Auth: scope by effective `user.id`, owner-isolation 404, no path from filename → Task 3 `_load_owned_session` + tests. ✓
- Validation: suffix+options on init, max_upload_bytes, offset 409, overflow 413 → Task 3 init/patch + tests. ✓
- Config (4 settings, env VTS_) → Task 2. ✓
- Client: loadUploadConfig in bootstrap, uploadFileChunked, determinate ring → Task 4. ✓
- Determinate progress (server-confirmed received/total) → Task 4 setProgress. ✓
- Tests: upload_session (tmp), uploads_api (Postgres incl resume/409/413/422/owner-404/incomplete-409), UI verifier → Tasks 1,3,5. ✓
- Version bump → Task 4. ✓
- GC out of scope → not implemented; tracked vts-ee3. ✓

**Placeholder scan:** No TBD/TODO. Two guarded "read the real thing" instructions remain (Task 4 `api()` integration, Task 3/5 selector confirmation) — these are genuine "verify against actual code before writing" directives with concrete fallbacks, not vague placeholders. The `filename` sidecar field is stored by `UploadSession.init` in Task 1 and read in Task 3 finalize, so `source_url` is correct and the muddled draft note was removed.

**Type consistency:** `UploadSession.init/load/part_path/meta_path/received_bytes/append_chunk/finalize` signatures match between Task 1 and Task 3. The init `filename` kwarg is defined in Task 1 (signature + sidecar + test) and passed by the Task 3 init endpoint and read in finalize — consistent across both. Schemas `UploadConfigOut/UploadInitRequest/UploadInitOut/UploadOffsetOut` consistent between Task 3 schema defs, endpoints, and tests. Settings names `upload_chunked_threshold_bytes/upload_chunk_bytes/max_upload_bytes/upload_session_ttl_seconds` consistent Task 2 ↔ Task 3 endpoint ↔ Task 4 client.
