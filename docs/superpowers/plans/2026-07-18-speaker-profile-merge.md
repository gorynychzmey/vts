# Speaker Profile Merge/Move + Name Substitution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user merge duplicate speaker profiles and move fragments between them in the voice registry, and substitute registry person names for "Голос N" in transcripts, processed transcripts, and summaries.

**Architecture:** Move/merge are repo operations over the existing `Speaker`/`VoiceSample`/`MatchDecision` schema (vts-80i): move changes `VoiceSample.speaker_id` and leaves history alone; merge additionally rewrites `MatchDecision.speaker_id` source→target before deleting the source. Name substitution resolves `speaker_label → Speaker.name` from `MatchDecision` in a DB-aware layer, then feeds that map to the existing pure render functions and to two new prompt template variables.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy async, Postgres+pgvector, vanilla JS frontend (`vts/static/app.js`), pytest.

## Global Constraints

- **Move vs merge differ on `MatchDecision`:** move NEVER touches it (calibration must not lie); merge rewrites `speaker_id` source→target BEFORE deleting the source (names survive in old tasks). Order matters — rewrite decisions, then delete source.
- **User isolation is a security boundary:** every speaker/sample/task access goes through user_id-scoped repo methods. Never let one user touch another's data.
- **Repo methods `flush()`, never `commit()`** — the caller (endpoint) owns the transaction.
- **Merge is one transaction, one commit** — reassign fragments + rewrite decisions + delete source are atomic.
- **`source_task_id` is preserved** on moved/merged fragments (origin fact, not ownership). FK already SET NULL.
- **No fragment dedup on merge** — a duplicate fragment only improves matching (MIN aggregation). YAGNI.
- **Name substitution is additive:** an unmatched voice (`speaker_id` NULL) or deleted person renders "Голос N" exactly as today. Undiarized tasks are unchanged.
- **Person names are not translated** — "Вася:" in any language; substitution bypasses `speaker_label_word`.
- **Participant list via prompt template vars** `${NAMED_SPEAKERS}` / `${ANONYMOUS_SPEAKERS}` — each a JSON array of names (empty `[]`); the prompt file states "(array may be empty)". Code serializes; the prompt interprets.
- **`app.js` has no `defer`** — new `getElementById` DOM blocks precede the `<script>` tag.
- **Bump `vts/__init__.py` before the last behavior-shipping commit.** Currently 1.4.2.
- **Thresholds/params in config, never hardcoded.** Move dropdown reuses `speaker_match_candidates_cap` (vts-4rt).

**Spec:** `docs/superpowers/specs/2026-07-18-speaker-profile-merge-design.md`

**Test env (from prior sessions — pytest hangs/errors without it):** Postgres = podman `vts-postgres-1` on `tensorchord/vchord-postgres:pg17-v1.1.1`, DB `vts_test` + `vector` extension, reachable at `127.0.0.1:5432` via the `vts-pg-fwd` socat forwarder. If tests ConnectionError: `podman rm -f vts-pg-fwd && docker run -d --rm --name vts-pg-fwd --network vts_default -p 127.0.0.1:5432:5432 alpine/socat:latest TCP-LISTEN:5432,fork,reuseaddr TCP:vts-postgres-1:5432`. Run: `/home/victor/dev/vts/.venv/bin/python -m pytest -q`. Baseline: 1088 passed.

---

## File Structure

**Modified:**
- `vts/db/repo.py` — `reassign_speaker_samples`, `move_voice_sample`, `merge_speakers`, `speaker_names_for_task`.
- `vts/api/main.py` — move + merge endpoints.
- `vts/api/schemas.py` — request models for move/merge.
- `vts/services/diarization/merge.py` — `label_map` accepts a name override map.
- `vts/pipeline/steps/transcription.py` — MergeTranscriptStep resolves names into the render.
- `vts/pipeline/steps/summarization.py` — replace `_keep_speakers_instruction` with participant-list vars; wire into `render_prompt_budget_vars`.
- `prompts/segment_prompt.md` + the final prompt — add `${NAMED_SPEAKERS}` / `${ANONYMOUS_SPEAKERS}`.
- `vts/static/index.html`, `app.js`, `styles.css` — move/merge UI in the existing registry dialog.

**New tests:** `tests/test_speaker_merge_move.py`, `tests/test_speaker_name_substitution.py`, plus additions to `tests/test_speaker_api.py`, `tests/test_diarization_merge.py`, `tests/test_diarization_prompt.py`.

---

## Task 1: Repo — reassign_speaker_samples primitive + move_voice_sample

**Files:**
- Modify: `vts/db/repo.py` (registry CRUD block, after `delete_voice_sample` ~line 652)
- Test: `tests/test_speaker_merge_move.py`

**Interfaces:**
- Consumes: `Speaker`, `VoiceSample`, `MatchDecision` models; `get_speaker(user_id, id)`, `get_voice_sample(user_id, id)` (vts-80i).
- Produces:
  - `reassign_speaker_samples(user_id, source_id, target_id) -> int` — sets `VoiceSample.speaker_id = target_id` for all of source's samples; returns count. Both speakers must belong to user_id.
  - `move_voice_sample(user_id, sample_id, target_speaker_id) -> VoiceSample | None` — sets one sample's `speaker_id`; None if sample or target not found / not user's. Does NOT touch MatchDecision.

- [ ] **Step 1: Write the failing test**

```python
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User, VoiceSample, MatchDecision
from vts.db.repo import Repo
from _db import make_test_engine, ensure_pgvector

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _mk_sample(repo, speaker_id, model="m1"):
    return await repo.add_voice_sample(
        speaker_id=speaker_id, embedding=[0.1] * 256, embedding_model=model,
        audio=b"x", audio_format="wav", duration_sec=5.0, source_task_id=None,
    )


@pytest.mark.asyncio
async def test_move_voice_sample_changes_speaker_not_decision(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        vs = await _mk_sample(repo, a.id)
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S0",
            speaker_id=a.id, voice_sample_id=vs.id, distance=0.1, embedding_model="m1", outcome="confirmed")
        await s.commit()

        moved = await repo.move_voice_sample(_USER, vs.id, b.id)
        await s.commit()
        assert moved is not None and moved.speaker_id == b.id
        # decision untouched — still points at A
        dec = (await s.scalars(select(MatchDecision))).one()
        assert dec.speaker_id == a.id


@pytest.mark.asyncio
async def test_move_voice_sample_isolation(factory):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    async with factory() as s:
        s.add(User(id=other, username="other"))
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        vs = await _mk_sample(repo, a.id)
        await s.commit()
        # target belongs to another user -> None
        assert await repo.move_voice_sample(other, vs.id, b.id) is None
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_merge_move.py -k move_voice_sample -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'move_voice_sample'`.

- [ ] **Step 3: Implement the methods**

In `vts/db/repo.py`, after `delete_voice_sample`, add (mirror its user-scoped style; `update` is already imported from sqlalchemy — verify):

```python
    async def reassign_speaker_samples(
        self, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID,
    ) -> int:
        """Move all of source's voice samples to target. Both must be the user's.

        Returns the number of samples reassigned. Does not touch MatchDecision —
        callers that need decision rewriting (merge) do it separately.
        """
        source = await self.get_speaker(user_id, source_id)
        target = await self.get_speaker(user_id, target_id)
        if source is None or target is None:
            return 0
        result = await self.session.execute(
            update(VoiceSample)
            .where(VoiceSample.speaker_id == source_id)
            .values(speaker_id=target_id)
        )
        await self.session.flush()
        return result.rowcount or 0

    async def move_voice_sample(
        self, user_id: uuid.UUID, sample_id: uuid.UUID, target_speaker_id: uuid.UUID,
    ) -> VoiceSample | None:
        """Reassign one sample to another of the user's speakers. None if not found."""
        sample = await self.get_voice_sample(user_id, sample_id)
        target = await self.get_speaker(user_id, target_speaker_id)
        if sample is None or target is None:
            return None
        sample.speaker_id = target_speaker_id
        await self.session.flush()
        return sample
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_speaker_merge_move.py -k move_voice_sample -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_speaker_merge_move.py
git commit -m "feat(db): move_voice_sample + reassign_speaker_samples (vts-552)"
```

---

## Task 2: Repo — merge_speakers

**Files:**
- Modify: `vts/db/repo.py`
- Test: `tests/test_speaker_merge_move.py`

**Interfaces:**
- Consumes: `reassign_speaker_samples` (Task 1), `get_speaker`, `delete_speaker`, `MatchDecision`.
- Produces: `merge_speakers(user_id, source_id, target_id) -> bool` — in order: reassign source's samples to target; rewrite `MatchDecision.speaker_id` source→target; delete source Speaker. False if source==target, or either not the user's.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speaker_merge_move.py`:
```python
from vts.db.models import Speaker


@pytest.mark.asyncio
async def test_merge_moves_samples_rewrites_decisions_deletes_source(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "Вася-1")
        b = await repo.create_speaker(_USER, "Вася-2")
        vs_a = await _mk_sample(repo, a.id)
        vs_b = await _mk_sample(repo, b.id)
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S0",
            speaker_id=a.id, voice_sample_id=vs_a.id, distance=0.1, embedding_model="m1", outcome="confirmed")
        await s.commit()

        assert await repo.merge_speakers(_USER, a.id, b.id) is True
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        # source gone
        assert await repo.get_speaker(_USER, a.id) is None
        # both samples now under b
        assert {v.speaker_id for v in await repo.list_voice_samples(b.id)} == {b.id}
        assert len(await repo.list_voice_samples(b.id)) == 2
        # decision rewritten a -> b
        dec = (await s.scalars(select(MatchDecision))).one()
        assert dec.speaker_id == b.id


@pytest.mark.asyncio
async def test_merge_same_speaker_false(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        await s.commit()
        assert await repo.merge_speakers(_USER, a.id, a.id) is False


@pytest.mark.asyncio
async def test_merge_isolation(factory):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    async with factory() as s:
        s.add(User(id=other, username="other"))
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        await s.commit()
        assert await repo.merge_speakers(other, a.id, b.id) is False
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_merge_move.py -k merge -v`
Expected: FAIL — no `merge_speakers`.

- [ ] **Step 3: Implement**

In `vts/db/repo.py`:
```python
    async def merge_speakers(
        self, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID,
    ) -> bool:
        """Merge source into target: samples + decisions move to target, source deleted.

        Order matters: rewrite decisions BEFORE deleting source, so the source
        delete's SET NULL finds no decisions pointing at it — names survive in old
        tasks. Merge asserts 'same person', so rewriting speaker_id does not distort
        the decisions' calibration (distance/outcome unchanged).
        """
        if source_id == target_id:
            return False
        source = await self.get_speaker(user_id, source_id)
        target = await self.get_speaker(user_id, target_id)
        if source is None or target is None:
            return False
        await self.reassign_speaker_samples(user_id, source_id, target_id)
        await self.session.execute(
            update(MatchDecision)
            .where(MatchDecision.user_id == user_id, MatchDecision.speaker_id == source_id)
            .values(speaker_id=target_id)
        )
        await self.session.delete(source)
        await self.session.flush()
        return True
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_speaker_merge_move.py -k merge -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_speaker_merge_move.py
git commit -m "feat(db): merge_speakers — reassign, rewrite decisions, delete source (vts-552)"
```

---

## Task 3: API — move + merge endpoints

**Files:**
- Modify: `vts/api/main.py` (near the other `/api/speakers` routes), `vts/api/schemas.py`
- Test: `tests/test_speaker_api.py`

**Interfaces:**
- Consumes: `move_voice_sample`, `merge_speakers` (Tasks 1-2); `get_current_user`, `get_session_dep`, `Repo` (vts-80i).
- Produces:
  - `POST /api/speakers/{speaker_id}/samples/{sample_id}/move` body `{target_speaker_id: UUID}` → 200 (moved sample summary) / 404.
  - `POST /api/speakers/{source_id}/merge` body `{target_id: UUID}` → 204 / 404 / 409 (source==target).

- [ ] **Step 1: Write the failing API tests**

Append to `tests/test_speaker_api.py` (uses the `client` fixture):
```python
@pytest.mark.asyncio
async def test_move_sample_via_api(client):
    a = (await client.post("/api/speakers", json={"name": "A"})).json()
    b = (await client.post("/api/speakers", json={"name": "B"})).json()
    # seed a sample under A via the registry (no direct add endpoint; use a helper
    # already used by other tests in this file to insert a VoiceSample for A) —
    # follow the existing pattern in test_speaker_api.py for creating a sample.
    # ... create sample S under A ...
    r = await client.post(f"/api/speakers/{a['id']}/samples/{S}/move",
                          json={"target_speaker_id": b["id"]})
    assert r.status_code == 200
    # A now has 0 samples, B has 1
    assert (await client.get(f"/api/speakers/{a['id']}/samples")).json() == []
    assert len((await client.get(f"/api/speakers/{b['id']}/samples")).json()) == 1


@pytest.mark.asyncio
async def test_merge_via_api(client):
    a = (await client.post("/api/speakers", json={"name": "Вася-1"})).json()
    b = (await client.post("/api/speakers", json={"name": "Вася-2"})).json()
    r = await client.post(f"/api/speakers/{a['id']}/merge", json={"target_id": b["id"]})
    assert r.status_code == 204
    names = [s["name"] for s in (await client.get("/api/speakers")).json()]
    assert "Вася-1" not in names and "Вася-2" in names


@pytest.mark.asyncio
async def test_merge_same_speaker_409(client):
    a = (await client.post("/api/speakers", json={"name": "A"})).json()
    r = await client.post(f"/api/speakers/{a['id']}/merge", json={"target_id": a["id"]})
    assert r.status_code == 409
```
(Implementer: for the move test's sample seeding, reuse whatever helper `test_speaker_api.py` already uses to insert a `VoiceSample` — do not invent a new one. If none exists, insert directly via the test's session factory, mirroring `tests/test_speaker_merge_move.py`'s `_mk_sample`.)

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_api.py -k "move_sample or merge" -v`
Expected: FAIL — 404/405 routing (no endpoint).

- [ ] **Step 3: Add schemas + endpoints**

In `vts/api/schemas.py`:
```python
class MoveVoiceSampleRequest(BaseModel):
    target_speaker_id: UUID


class MergeSpeakersRequest(BaseModel):
    target_id: UUID
```
In `vts/api/main.py`, near the other `/api/speakers` routes (mirror their auth + `Repo(session)` + `await session.commit()` shape):
```python
    @app.post("/api/speakers/{speaker_id}/samples/{sample_id}/move", response_model=VoiceSampleOut)
    async def move_voice_sample_endpoint(
        speaker_id: uuid.UUID, sample_id: uuid.UUID, payload: MoveVoiceSampleRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> VoiceSampleOut:
        repo = Repo(session)
        # verify the sample currently belongs to speaker_id (consistency, mirrors
        # the vts-4rt delete fix), then move it
        sample = await repo.get_voice_sample(uuid.UUID(user.id), sample_id)
        if sample is None or sample.speaker_id != speaker_id:
            raise HTTPException(status_code=404, detail="Voice sample not found")
        moved = await repo.move_voice_sample(uuid.UUID(user.id), sample_id, payload.target_speaker_id)
        if moved is None:
            raise HTTPException(status_code=404, detail="Target speaker not found")
        await session.commit()
        return VoiceSampleOut(id=moved.id, duration_sec=moved.duration_sec,
                              source_task_id=moved.source_task_id, created_at=moved.created_at)

    @app.post("/api/speakers/{source_id}/merge", status_code=204)
    async def merge_speakers_endpoint(
        source_id: uuid.UUID, payload: MergeSpeakersRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> Response:
        if source_id == payload.target_id:
            raise HTTPException(status_code=409, detail="Cannot merge a speaker into itself")
        repo = Repo(session)
        ok = await repo.merge_speakers(uuid.UUID(user.id), source_id, payload.target_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Speaker not found")
        await session.commit()
        return Response(status_code=204)
```
(Check the exact `VoiceSampleOut` field set in schemas.py — match it; the fields above mirror the GET samples endpoint.)

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_speaker_api.py -k "move_sample or merge" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py vts/api/schemas.py tests/test_speaker_api.py
git commit -m "feat(api): move-sample and merge-speaker endpoints (vts-552)"
```

---

## Task 4: Repo — speaker_names_for_task (label → name map)

**Files:**
- Modify: `vts/db/repo.py`
- Test: `tests/test_speaker_name_substitution.py`

**Interfaces:**
- Consumes: `MatchDecision`, `Speaker`.
- Produces: `speaker_names_for_task(user_id, task_id) -> dict[str, str]` — `{speaker_label: Speaker.name}` for every decision of this task whose `speaker_id` resolves to an existing speaker. Labels with no decision, or whose speaker was deleted (`speaker_id` NULL), are absent from the map.

- [ ] **Step 1: Write the failing test**

Create `tests/test_speaker_name_substitution.py`:
```python
import uuid
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User, Task
from vts.db.repo import Repo
from _db import make_test_engine, ensure_pgvector

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_speaker_names_for_task(factory):
    task_id = uuid.uuid4()
    async with factory() as s:
        repo = Repo(s)
        # a task row is needed for the FK
        s.add(Task(id=task_id, user_id=_USER, source_url="x", artifact_dir="/tmp/x",
                   options={}, status="completed"))
        vasya = await repo.create_speaker(_USER, "Вася")
        await repo.record_decision(user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=vasya.id, voice_sample_id=None, distance=0.1, embedding_model="m", outcome="confirmed")
        # a left-anonymous decision (speaker_id None) must NOT appear
        await repo.record_decision(user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_01",
            speaker_id=None, voice_sample_id=None, distance=None, embedding_model="m", outcome="left_anonymous")
        await s.commit()

        names = await repo.speaker_names_for_task(_USER, task_id)
        assert names == {"SPEAKER_00": "Вася"}
```
(Check the `Task` model's required columns in `vts/db/models.py` and adjust the seed row to satisfy NOT NULL constraints — status may be an enum value, artifact_dir/options/source_url required.)

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_speaker_name_substitution.py -k names_for_task -v`
Expected: FAIL — no `speaker_names_for_task`.

- [ ] **Step 3: Implement**

In `vts/db/repo.py`:
```python
    async def speaker_names_for_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> dict[str, str]:
        """Map speaker_label -> current Speaker.name for this task's matched voices.

        Join decisions to speakers so a deleted person (speaker_id SET NULL, or row
        gone) simply drops out — the caller renders "Голос N" for absent labels.
        The latest decision per (task, label) wins if several exist.
        """
        stmt = (
            select(MatchDecision.speaker_label, Speaker.name, MatchDecision.created_at)
            .join(Speaker, MatchDecision.speaker_id == Speaker.id)
            .where(MatchDecision.user_id == user_id, MatchDecision.source_task_id == task_id)
            .order_by(MatchDecision.created_at.asc())
        )
        rows = await self.session.execute(stmt)
        # asc order means the last write for a label wins
        result: dict[str, str] = {}
        for label, name, _ in rows.all():
            result[str(label)] = str(name)
        return result
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_speaker_name_substitution.py -k names_for_task -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_speaker_name_substitution.py
git commit -m "feat(db): speaker_names_for_task — label->name map for rendering (vts-552)"
```

---

## Task 5: label_map accepts a name override

**Files:**
- Modify: `vts/services/diarization/merge.py` (`label_map` ~line 550, `render_transcript` ~605)
- Test: `tests/test_diarization_merge.py`

**Interfaces:**
- Consumes: nothing new (pure function).
- Produces: `label_map(entries, label_word="Голос", names=None) -> dict[str,str]` — when `names` (a `{speaker_label: person_name}` dict) is given, a label present in `names` maps to the person name; absent labels keep the "Голос N"/"Speaker N" numbering. `names=None` = current behavior, unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_diarization_merge.py`:
```python
from vts.services.diarization.merge import label_map


def test_label_map_substitutes_names_where_present():
    entries = [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_01"}, {"speaker": "SPEAKER_00"}]
    m = label_map(entries, "Голос", names={"SPEAKER_00": "Вася"})
    assert m["SPEAKER_00"] == "Вася"       # named
    assert m["SPEAKER_01"] == "Голос 2"    # unnamed keeps numbering


def test_label_map_none_names_is_current_behavior():
    entries = [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_01"}]
    m = label_map(entries, "Голос")
    assert m == {"SPEAKER_00": "Голос 1", "SPEAKER_01": "Голос 2"}
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_diarization_merge.py -k label_map_substitutes -v`
Expected: FAIL — `label_map() got an unexpected keyword argument 'names'`.

- [ ] **Step 3: Implement**

Read the current `label_map` body first. Add a `names: dict[str, str] | None = None` parameter; when building the map, if a label is in `names`, use that value instead of the numbered "Голос N". Keep the numbering counter for unnamed labels exactly as now (so "SPEAKER_01" still becomes "Голос 2", not "Голос 1"). Thread the same optional `names` through `render_transcript`'s signature so callers can pass it (default None keeps current behavior).

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_diarization_merge.py -k "label_map" -v`
Expected: PASS (both new + existing label_map tests).

- [ ] **Step 5: Commit**

```bash
git add vts/services/diarization/merge.py tests/test_diarization_merge.py
git commit -m "feat(diarization): label_map optional person-name override (vts-552)"
```

---

## Task 6: MergeTranscriptStep renders names into the raw transcript

**Files:**
- Modify: `vts/pipeline/steps/transcription.py` (the render call ~line 212)
- Test: `tests/test_diarization_transcript.py`

**Interfaces:**
- Consumes: `speaker_names_for_task` (Task 4), `label_map(..., names=...)` (Task 5).
- Produces: the rendered raw transcript prints person names for matched voices, "Голос N" otherwise.

- [ ] **Step 1: Write the failing test**

Add a test to `tests/test_diarization_transcript.py` that runs the transcript-render path (or the helper it calls) with a stub `speaker_names_for_task` returning `{"SPEAKER_00": "Вася"}` and asserts the rendered text contains "Вася:" for SPEAKER_00 and "Голос 2:" for an unnamed SPEAKER_01. (Mirror how the existing tests in this file drive `MergeTranscriptStep` / the render helper; use its existing fixtures.)

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_diarization_transcript.py -k names -v`
Expected: FAIL (names not substituted yet).

- [ ] **Step 3: Implement**

In `vts/pipeline/steps/transcription.py`, where the render happens (~line 212, `mapping = label_map(cleaned, speaker_label_word(language))`): before rendering, resolve names via `speaker_names_for_task(user_id, task_id)` (open a repo session as other steps do — see how the step accesses `ctx.session_factory`/`Repo`), and pass them: `mapping = label_map(cleaned, speaker_label_word(language), names=names)`. The step already has `task_id` and `user_id` in scope (StepState). If the render helper is a module-level function without DB access, resolve the names in the step and pass the dict down as a parameter — keep the pure function pure.

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_diarization_transcript.py -k names -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/transcription.py tests/test_diarization_transcript.py
git commit -m "feat(pipeline): raw transcript renders registry names for matched voices (vts-552)"
```

---

## Task 7: Utterance splitter recognizes named labels

**Files:**
- Modify: `vts/services/diarization/merge.py` (`_UTTERANCE_RE` and any label-recognition regex)
- Test: `tests/test_diarization_merge.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the utterance splitter treats an arbitrary "Name:" at line start as a speaker label, not only dictionary "Голос N:"/"Speaker N:".

- [ ] **Step 1: Investigate + write the failing test**

First READ `_UTTERANCE_RE` in `merge.py`. If it matches only "Голос"/"Speaker" + number, a named transcript ("Вася: ...\nПетя: ...") will NOT split into utterances — the dialogue render breaks. Add a test:
```python
from vts.services.diarization.merge import split_utterances  # or the actual splitter name


def test_split_recognizes_named_labels():
    text = "Вася: привет.\nПетя: здравствуй."
    parts = split_utterances(text)
    assert len(parts) == 2
```
(Use the real splitter function name from the file. If the splitter is only used internally, test through the smallest public function that invokes it.)

- [ ] **Step 2: Run, verify failure (or pass — investigate)**

Run: `pytest tests/test_diarization_merge.py -k named_labels -v`
Expected: FAIL if the regex is dictionary-only. **If it already PASSES** (regex is generic "word(s): " at line start), this task is a no-op — record that in the commit and skip the implementation step, keeping the test as a regression guard.

- [ ] **Step 3: Implement (only if the test failed)**

Broaden the label-recognition regex to match a name label at line start: a run of non-newline, non-colon characters followed by ": " at the start of a line, capped to a reasonable length (labels aren't sentences). Be careful NOT to match ordinary prose containing a colon mid-sentence — anchor to line start and keep it a plausible-name shape (e.g. `^[^\n:]{1,40}: `). Verify existing merge tests still pass (the change must not break "Голос N:"/"Speaker N:" recognition).

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_diarization_merge.py -v`
Expected: PASS (new + all existing).

- [ ] **Step 5: Commit**

```bash
git add vts/services/diarization/merge.py tests/test_diarization_merge.py
git commit -m "feat(diarization): utterance splitter recognizes named speaker labels (vts-552)"
```

---

## Task 8: Participant-list prompt variables replace keep-speakers instruction

**Files:**
- Modify: `vts/pipeline/steps/summarization.py` (`_keep_speakers_instruction` ~71, `rewrite_prompt` ~90, `render_prompt_budget_vars` ~169), `prompts/segment_prompt.md`, the final prompt file
- Test: `tests/test_diarization_prompt.py`

**Interfaces:**
- Consumes: `speaker_names_for_task` (Task 4).
- Produces:
  - `participant_vars(named: list[str], anonymous: list[str]) -> dict[str,str]` — returns `{"NAMED_SPEAKERS": json.dumps(named, ensure_ascii=False), "ANONYMOUS_SPEAKERS": json.dumps(anonymous, ensure_ascii=False)}`.
  - **`render_prompt_vars(...)`** — `render_prompt_budget_vars` RENAMED (it now substitutes language + budget + participants, so "budget_vars" no longer describes it). Gains optional `named_speakers`/`anonymous_speakers` lists and substitutes `${NAMED_SPEAKERS}`/`${ANONYMOUS_SPEAKERS}`.
  - `${NAMED_SPEAKERS}` / `${ANONYMOUS_SPEAKERS}` present in `segment_prompt.md` + final prompt.

**Rename note:** `render_prompt_budget_vars` has exactly 5 call sites, ALL inside `vts/pipeline/steps/summarization.py` (lines 632, 825, 923, 1145, plus the def at 169), and NO test references the name (verified by grep). So the rename to `render_prompt_vars` is a mechanical same-file replace with no test fallout — do it as part of this task, not separately. Do NOT rename the helpers it calls (`render_prompt_with_language`, `inject_budget_vars`) — only the outer wrapper.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_diarization_prompt.py`:
```python
import json
from vts.pipeline.steps.summarization import participant_vars


def test_participant_vars_json_arrays():
    v = participant_vars(["Вася", "Петя"], ["Голос 2"])
    assert json.loads(v["NAMED_SPEAKERS"]) == ["Вася", "Петя"]
    assert json.loads(v["ANONYMOUS_SPEAKERS"]) == ["Голос 2"]


def test_participant_vars_empty():
    v = participant_vars([], [])
    assert v["NAMED_SPEAKERS"] == "[]" and v["ANONYMOUS_SPEAKERS"] == "[]"
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/test_diarization_prompt.py -k participant_vars -v`
Expected: FAIL — no `participant_vars`.

- [ ] **Step 3: Implement + wire + edit prompts**

Add `participant_vars` to `summarization.py`. Rename `render_prompt_budget_vars` → `render_prompt_vars` at the def and all 5 call sites (see the Rename note above). Replace `_keep_speakers_instruction`'s role: `rewrite_prompt` no longer appends the quoted-label instruction; instead the participant vars are substituted into the prompt (which now contains the wording). In `render_prompt_vars`, add optional `named_speakers`/`anonymous_speakers` params and, after the `${LANG}` substitution, do `for k, val in participant_vars(named, anon).items(): prompt = prompt.replace(f"${{{k}}}", val)`. The step that builds the prompt resolves named/anonymous from `speaker_names_for_task` + the diarization labels (named = values present; anonymous = labels not in the names map, rendered "Голос N").

In `prompts/segment_prompt.md` (and the final prompt file), add near the top:
```
Список участников диалога (массив JSON, может быть пустым): ${NAMED_SPEAKERS}.
Список анонимных участников (массив JSON, может быть пустым): ${ANONYMOUS_SPEAKERS}.
```
Remove reliance on `_keep_speakers_instruction`; if any test asserts its exact old text, update it. Keep the diarized-vs-undiarized behavior: undiarized tasks pass `[]`/`[]` (the lines render with empty arrays, which the wording explicitly allows).

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_diarization_prompt.py -v`
Expected: PASS (new + updated existing).

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/summarization.py prompts/segment_prompt.md tests/test_diarization_prompt.py
git commit -m "feat(summary): participant-list prompt vars replace keep-speakers instruction (vts-552)"
```

---

## Task 9: Wire participant list into the summarization pipeline (both paths)

**Files:**
- Modify: `vts/pipeline/steps/summarization.py` (the window-summarize + overflow-re-chunk paths)
- Test: `tests/test_segmentation_mode.py` or `tests/test_diarization_prompt.py`

**Interfaces:**
- Consumes: `participant_vars`, `speaker_names_for_task`, `render_prompt_vars(named_speakers=..., anonymous_speakers=...)` (renamed in Task 8).
- Produces: both the main window-summarization prompt build AND the overflow re-chunk path (vts-5xz) pass the participant lists, so a regenerated summary carries names.

- [ ] **Step 1: Write the failing test**

Add a test that drives the prompt-building path with a task whose `speaker_names_for_task` returns `{"SPEAKER_00": "Вася"}` and whose diarization has SPEAKER_00 + SPEAKER_01, and asserts the built prompt contains `["Вася"]` in the NAMED slot and `["Голос 2"]` (or the unnamed label) in ANONYMOUS. Mirror how existing tests in the chosen file build a prompt.

- [ ] **Step 2: Run, verify failure**

Run: `pytest -k participant_wiring -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Find where `render_prompt_vars` (renamed in Task 8) / `rewrite_prompt` are called in the summarize path (~line 413 and ~511 — the main path and the overflow re-chunk). At BOTH sites, resolve named/anonymous from `speaker_names_for_task(user_id, task_id)` plus the task's diarization labels, and pass them. This mirrors vts-5xz's Task-8 finding that the overflow re-chunk path must be wired identically or the feature silently stops on long transcripts.

- [ ] **Step 4: Run, verify pass**

Run: `pytest -k participant_wiring -v` then the full diarization suite `pytest tests/test_diarization_prompt.py tests/test_segmentation_mode.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/summarization.py tests/
git commit -m "feat(summary): pass participant list on both summarize paths (vts-552)"
```

---

## Task 10: Registry dialog — move-fragment UI

**Files:**
- Modify: `vts/static/index.html`, `vts/static/app.js`, `vts/static/styles.css`
- Verify: `verifier-web` skill

**Interfaces:**
- Consumes: `POST /api/speakers/{id}/samples/{sid}/move` (Task 3), `GET /api/speakers` (vts-80i).

- [ ] **Step 1: Add a "переместить" button per fragment**

In the registry dialog's right column (fragment list — added in vts-80i task 13), add a move button next to each fragment's delete button. Reuse the existing fragment-row rendering in `app.js`.

- [ ] **Step 2: Move dropdown reusing the resolution-dialog component**

On click, open a dropdown of the user's speakers with `<Создать новую персону>` FIRST, then speakers sorted by embedding distance to this fragment by default, with a toggle to alphabetical. "Создать новую" stays first in both orders. Reuse the dropdown component built in vts-80i task 14 (the voice-resolution dialog's all-candidates select); distance ranking uses the same `nearest_speakers` data path, capped by `speaker_match_candidates_cap`. On selecting an existing speaker → confirm "Фрагмент будет перемещён к персоне «X». Продолжить?" → POST move. On "Создать новую" → name input → create speaker then move (the endpoint chain; or a create-then-move in the client).

- [ ] **Step 3: Confirm + refresh**

Every move confirms (project pattern). After a successful move, refresh the fragment list (the fragment left this person).

- [ ] **Step 4: Verify in a browser**

Run `verifier-web`: stub `/api/speakers*` and the move endpoint; assert the move button appears, the dropdown lists speakers with "Создать новую" first and a sort toggle, the confirmation fires, and the list refreshes.

- [ ] **Step 5: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css
git commit -m "feat(ui): move a fragment to another person in the registry dialog (vts-552)"
```

---

## Task 11: Registry dialog — merge-persons UI

**Files:**
- Modify: `vts/static/index.html`, `vts/static/app.js`, `vts/static/styles.css`
- Verify: `verifier-web`

**Interfaces:**
- Consumes: `POST /api/speakers/{source}/merge` (Task 3), `GET /api/speakers`.

- [ ] **Step 1: Add a "слить" button per person**

In the left column (person list), add a merge button next to each person's rename/delete buttons. The button sits on the SOURCE (the person that will disappear).

- [ ] **Step 2: Target picker + directional confirmation**

On click, show a picker of the other speakers (alphabetical; "Создать новую" is NOT offered here — merge targets an existing person). On selecting a target, confirm with the exact directional text: "Все голосовые данные персоны «Вася-1» будут перенесены в «Вася-2». Персона «Вася-1» будет удалена. Продолжить?" (interpolate both names). POST merge on confirm.

- [ ] **Step 3: Refresh + selection**

After merge, refresh the person list (source gone). If the currently-selected person was the source, select the target instead.

- [ ] **Step 4: Verify in a browser**

Run `verifier-web`: stub `/api/speakers` and the merge endpoint; assert the merge button appears, the target picker excludes "Создать новую", the confirmation shows both names with correct direction, and the list refreshes with the source gone.

- [ ] **Step 5: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css
git commit -m "feat(ui): merge two persons in the registry dialog (vts-552)"
```

---

## Task 12: End-to-end + version bump + docs

**Files:**
- Modify: `vts/__init__.py` (version bump), docs for the new endpoints
- Test: full suite

- [ ] **Step 1: Run the full suite**

Run: `/home/victor/dev/vts/.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 2: Document the new endpoints + naming behavior**

Add the move/merge endpoints and the name-substitution behavior (raw transcript at render, summary on regeneration) to the API/architecture docs where the vts-80i registry endpoints are documented.

- [ ] **Step 3: Bump version**

Bump `vts/__init__.py` 1.4.2 → 1.5.0 (feature release).

- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py docs/
git commit -m "chore(release): speaker profile merge/move + naming — docs, version bump (vts-552)"
```

---

## Self-Review Notes

- **Spec coverage:** move (T1/T3), merge (T2/T3), name map (T4), transcript render (T5/T6), utterance splitter (T7), participant prompt vars (T8), both summarize paths (T9), move UI (T10), merge UI (T11), docs+version (T12). The move-vs-merge MatchDecision rule is enforced in T1 (move leaves it) and T2 (merge rewrites, order-checked). Transactionality: each endpoint commits once (T3). Isolation: every repo method user-scoped (T1/T2/T4), tested. Out-of-scope items (dedup, undo, aliases, multi-select, auto-regeneration) are not implemented — correct.
- **Investigation tasks flagged inline:** T7 is conditional (the splitter regex may already be generic — the test reveals it; no-op if it passes). T6/T9 note the pure-function-keeps-DB-out boundary and the vts-5xz overflow-path lesson.
- **Deferred/uncertain, noted for the implementer:** the exact `VoiceSampleOut` field set (T3), the `Task` seed row's NOT NULL columns (T4), the real splitter function name (T7), and the final prompt file's path (T8) — each says "read the file / match the existing shape" rather than inventing.
