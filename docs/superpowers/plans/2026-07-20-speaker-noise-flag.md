# Speaker "noise" flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the irreversible `drop_marginal_speakers` auto-fold with a reversible per-speaker "noise" flag (auto-suggested by embedding+share, operator-editable), and let the operator resolve/edit speaker bindings any time after `match_speakers` runs (not only at the pause), re-rendering the raw transcript on save.

**Architecture:** Auto-noise is computed in `MatchSpeakersStep` and written into `speaker_matches.json`. The operator's decision persists in a new `MatchDecision.is_noise` column. A shared `rerender_transcript(task, session)` function (called from the resolve endpoint, NOT a DAG step) re-renders `transcript.json`/`transcript.txt` from the stored entries, dropping noise labels and substituting names; because the summary reads its text and participants from `transcript.json`, it inherits the exclusion automatically. Dialog availability is a new task-DEPENDENT capability `can_resolve_speakers`.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, pytest (real Postgres), vanilla JS frontend, Playwright verifier (`tests/ui`).

## Global Constraints

- Bump `vts/__init__.py` `__version__` (patch) before committing the final task — do NOT create a build tag (build only on explicit request).
- Tests run against **real Postgres** (`VTS_TEST_DATABASE_URL`, default `postgresql+asyncpg://vts:vts@localhost:5432/vts_test`); there is no SQLite fallback.
- `app.js` has NO `defer`: any element referenced by `getElementById` must appear in `index.html` BEFORE the `<script>` tag. (Not expected here — reusing existing dialog elements.)
- New settings use `env_prefix="VTS_"` and, for the consolidated services section, a `services_...` alias entry in `Settings.services_aliases`.
- Frontend UI changes require a `tests/ui` verifier scenario; run `cd tests/ui && node run.mjs` before the final commit when `vts/static/*` changed.
- Cosine distance is the only valid metric on these embeddings (vectors are unnormalised).
- Migration head is currently `0018_task_status_awaiting_input`; the new migration's `down_revision` is `"0018_task_status_awaiting_input"`.

---

### Task 1: New setting `diarization_noise_max_distance`

**Files:**
- Modify: `vts/core/config.py:105-106` (add setting near `diarization_min_speaker_share`), and `Settings.services_aliases` (~`vts/core/config.py:437-442`)
- Test: `tests/test_config.py` (create if absent; otherwise append)

**Interfaces:**
- Produces: `Settings.diarization_noise_max_distance: float` (default `0.25`), env `VTS_DIARIZATION_NOISE_MAX_DISTANCE`, services alias `services_diarization_noise_max_distance`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (create the file with this content if it does not exist):

```python
from vts.core.config import Settings


def test_diarization_noise_max_distance_default():
    s = Settings()
    assert s.diarization_noise_max_distance == 0.25


def test_diarization_noise_max_distance_env(monkeypatch):
    monkeypatch.setenv("VTS_DIARIZATION_NOISE_MAX_DISTANCE", "0.3")
    s = Settings()
    assert s.diarization_noise_max_distance == 0.3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -q -p no:warnings`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'diarization_noise_max_distance'`

- [ ] **Step 3: Add the setting**

In `vts/core/config.py`, immediately after the `diarization_min_speaker_share` line (currently line 105):

```python
    diarization_min_speaker_share: float = 0.05
    # Cosine distance below which a low-share speaker is auto-flagged as noise
    # (echo / a real speaker's own voice cut on a pause), if it is also close to
    # some LARGER-share speaker. Separate from speaker_match_max_distance_auto:
    # that answers "same voice in the registry", this answers "echo of another
    # speaker in THIS recording". Never folds a speaker with no embedding, and
    # never folds a large-share speaker no matter how close (vts-552 / vts-0ws).
    diarization_noise_max_distance: float = 0.25
```

In `Settings.services_aliases` (after the `services_diarization_min_speaker_share` entry):

```python
        "services_diarization_min_speaker_share": "diarization_min_speaker_share",
        "services_diarization_noise_max_distance": "diarization_noise_max_distance",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/core/config.py tests/test_config.py
git commit -m "feat(config): diarization_noise_max_distance setting (vts-552)"
```

---

### Task 2: Migration + model column `MatchDecision.is_noise`

**Files:**
- Create: `alembic/versions/0019_match_decision_is_noise.py`
- Modify: `vts/db/models.py:260` (add column after `outcome`)
- Test: `tests/test_speaker_registry_repo.py` (append) — a decision round-trips `is_noise`

**Interfaces:**
- Produces: `MatchDecision.is_noise: Mapped[bool]` (NOT NULL, default `False`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speaker_registry_repo.py`:

This file uses a module-level `_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")` and a `factory` fixture (`async with factory() as s`). Match that pattern:

```python
@pytest.mark.asyncio
async def test_record_decision_persists_is_noise(factory):
    from vts.db.repo import Repo
    async with factory() as s:
        repo = Repo(s)
        row = await repo.record_decision(
            user_id=_USER,
            source_task_id=None,
            speaker_label="SPEAKER_01",
            speaker_id=None,
            voice_sample_id=None,
            distance=None,
            embedding_model="m",
            outcome="left_anonymous",
            is_noise=True,
        )
        assert row.is_noise is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_speaker_registry_repo.py::test_record_decision_persists_is_noise -q -p no:warnings`
Expected: FAIL — `TypeError: record_decision() got an unexpected keyword argument 'is_noise'` (and/or missing column)

- [ ] **Step 3: Add the model column**

In `vts/db/models.py`, in `class MatchDecision`, after the `outcome` line (currently line 260):

```python
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    is_noise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
```

`Boolean` and `text` are NOT currently imported in `vts/db/models.py`. Add them to the existing `from sqlalchemy import (` block (which already imports `Float`, `Integer`, `String`, etc.) — add `Boolean,` and `text,` alphabetically. Verify:

```bash
grep -nE "Boolean|[^_a-zA-Z]text," vts/db/models.py | head
```
Expected: both names appear in the import block.

- [ ] **Step 4: Create the migration**

Create `alembic/versions/0019_match_decision_is_noise.py`:

```python
"""Add MatchDecision.is_noise (vts-552)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0019_match_decision_is_noise"
down_revision = "0018_task_status_awaiting_input"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "match_decisions",
        sa.Column("is_noise", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("match_decisions", "is_noise")
```

- [ ] **Step 5: Add `is_noise` param to `record_decision`**

In `vts/db/repo.py`, `record_decision` (currently lines 809-821), add the parameter and pass it through:

```python
    async def record_decision(
        self, *, user_id: uuid.UUID, source_task_id: uuid.UUID | None, speaker_label: str,
        speaker_id: uuid.UUID | None, voice_sample_id: uuid.UUID | None,
        distance: float | None, embedding_model: str, outcome: str,
        is_noise: bool = False,
    ) -> MatchDecision:
        row = MatchDecision(
            user_id=user_id, source_task_id=source_task_id, speaker_label=speaker_label,
            speaker_id=speaker_id, voice_sample_id=voice_sample_id, distance=distance,
            embedding_model=embedding_model, outcome=outcome, is_noise=is_noise,
        )
        self.session.add(row)
        await self.session.flush()
        return row
```

- [ ] **Step 6: Run migration against the test DB, then run the test**

The test harness builds the schema with `create_all`, so the model column alone makes the test pass; still verify the migration applies cleanly against a scratch DB:

```bash
grep -q "0019_match_decision_is_noise" alembic/versions/0019_match_decision_is_noise.py && echo "migration file ok"
.venv/bin/python -m pytest tests/test_speaker_registry_repo.py::test_record_decision_persists_is_noise -q -p no:warnings
```
Expected: `migration file ok`, then PASS

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0019_match_decision_is_noise.py vts/db/models.py vts/db/repo.py tests/test_speaker_registry_repo.py
git commit -m "feat(db): MatchDecision.is_noise column + record_decision param (vts-552)"
```

---

### Task 3: `noise_labels_for_task` resolver in Repo

**Files:**
- Modify: `vts/db/repo.py` (add method after `speaker_names_for_task`, ~line 755)
- Test: `tests/test_speaker_registry_repo.py` (append)

**Interfaces:**
- Consumes: `MatchDecision.is_noise` (Task 2).
- Produces: `Repo.noise_labels_from_decisions(user_id, task_id) -> set[str]` — labels whose LATEST decision has `is_noise=True`. Returns `set()` when no decisions exist (caller then falls back to `speaker_matches.json`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speaker_registry_repo.py`:

```python
@pytest.mark.asyncio
async def test_noise_labels_from_decisions(factory):
    from vts.db.repo import Repo
    task_id = uuid.uuid4()
    async with factory() as s:
        repo = Repo(s)
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=None, voice_sample_id=None, distance=None,
            embedding_model="m", outcome="left_anonymous", is_noise=False,
        )
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_01",
            speaker_id=None, voice_sample_id=None, distance=None,
            embedding_model="m", outcome="left_anonymous", is_noise=True,
        )
        await s.commit()
        labels = await repo.noise_labels_from_decisions(_USER, task_id)
        assert labels == {"SPEAKER_01"}


@pytest.mark.asyncio
async def test_noise_labels_empty_when_no_decisions(factory):
    from vts.db.repo import Repo
    async with factory() as s:
        repo = Repo(s)
        labels = await repo.noise_labels_from_decisions(_USER, uuid.uuid4())
        assert labels == set()
```

Note: `record_decision` writes `source_task_id` as a real FK to `tasks` (ondelete SET NULL, nullable) — but `test_noise_labels_from_decisions` uses a random `task_id` not present in `tasks`. The column is nullable with `SET NULL`, and there is no NOT-NULL/existence constraint enforced at insert for a nullable FK in Postgres ONLY when the value is NULL; a non-null value DOES require the row to exist. So either (a) insert a `Task` row with `id=task_id` first (mirror how other tests seed a task), or (b) pass `source_task_id=None` and store the label uniqueness another way. Prefer (a): seed a minimal `Task` row before recording decisions. Read `tests/test_speaker_api.py` for the minimal Task fields, and add the seed inside the `async with factory() as s` block before the decisions.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_speaker_registry_repo.py -k noise_labels -q -p no:warnings`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'noise_labels_from_decisions'`

- [ ] **Step 3: Implement the method**

In `vts/db/repo.py`, after `speaker_names_for_task` (ends ~line 755):

```python
    async def noise_labels_from_decisions(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> set[str]:
        """Labels whose LATEST decision for this task is is_noise=True.

        Empty when no decisions exist for the task — the caller then falls back
        to the auto-suggestion in speaker_matches.json (auto mode). Latest wins
        per label: ordered so the last decision in a re-save overrides earlier
        ones, mirroring speaker_names_for_task.
        """
        stmt = (
            select(MatchDecision.speaker_label, MatchDecision.is_noise)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == task_id,
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        rows = await self.session.execute(stmt)
        latest: dict[str, bool] = {}
        for label, is_noise in rows.all():
            latest[str(label)] = bool(is_noise)
        return {label for label, is_noise in latest.items() if is_noise}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_speaker_registry_repo.py -k noise_labels -q -p no:warnings`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add vts/db/repo.py tests/test_speaker_registry_repo.py
git commit -m "feat(db): noise_labels_from_decisions resolver (vts-552)"
```

---

### Task 4: Auto-noise + share computation (pure functions)

**Files:**
- Modify: `vts/services/diarization/merge.py` (add two pure functions near the top-level helpers, e.g. after `speaker_at`/`nearest_speaker`, ~line 245)
- Test: `tests/test_diarization_merge.py` (append)

**Interfaces:**
- Produces:
  - `speaker_shares(diar_segments: list[dict]) -> dict[str, float]` — per-speaker share of total diarized time (0..1). Empty dict if no segments / zero total.
  - `auto_noise_labels(shares: dict[str, float], embeddings: dict[str, list[float]], min_share: float, max_distance: float) -> set[str]` — labels that are `share < min_share` AND have an embedding AND are within `max_distance` cosine of some larger-share speaker.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diarization_merge.py`:

```python
from vts.services.diarization.merge import speaker_shares, auto_noise_labels


def test_speaker_shares_by_diarization_time():
    segs = [
        {"start": 0.0, "end": 10.0, "speaker": "A"},
        {"start": 10.0, "end": 12.0, "speaker": "B"},
        {"start": 12.0, "end": 20.0, "speaker": "A"},
    ]
    shares = speaker_shares(segs)
    # A = 18s, B = 2s, total 20s
    assert abs(shares["A"] - 0.9) < 1e-9
    assert abs(shares["B"] - 0.1) < 1e-9


def test_speaker_shares_empty():
    assert speaker_shares([]) == {}


def test_auto_noise_close_and_small_is_noise():
    shares = {"A": 0.95, "B": 0.05}
    # B is tiny AND its embedding is identical to A -> echo -> noise
    emb = {"A": [1.0, 0.0], "B": [1.0, 0.0]}
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == {"B"}


def test_auto_noise_far_and_small_is_not_noise():
    shares = {"A": 0.95, "B": 0.05}
    # B is tiny but acoustically distinct from A (orthogonal -> cosine dist 1.0)
    emb = {"A": [1.0, 0.0], "B": [0.0, 1.0]}
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()


def test_auto_noise_large_speaker_never_noise():
    shares = {"A": 0.60, "B": 0.40}
    emb = {"A": [1.0, 0.0], "B": [1.0, 0.0]}  # identical, but B is large-share
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()


def test_auto_noise_no_embedding_never_noise():
    shares = {"A": 0.95, "B": 0.05}
    emb = {"A": [1.0, 0.0]}  # B has no embedding
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_diarization_merge.py -k "shares or auto_noise" -q -p no:warnings`
Expected: FAIL — `ImportError: cannot import name 'speaker_shares'`

- [ ] **Step 3: Implement the functions**

In `vts/services/diarization/merge.py`, after `nearest_speaker` (~line 245):

```python
def speaker_shares(diar_segments: list[dict[str, Any]]) -> dict[str, float]:
    """Per-speaker share of total DIARIZED time (0..1).

    Uses diarization segment durations, NOT merged ASR-entry spans — a speaker
    whose turns are many short interjections keeps its true share here, which is
    the fix for vts-0ws (drop_marginal folded a real 13% speaker it measured as
    3% by ASR-entry span).
    """
    totals: dict[str, float] = {}
    for seg in diar_segments:
        speaker = str(seg["speaker"])
        totals[speaker] = totals.get(speaker, 0.0) + (float(seg["end"]) - float(seg["start"]))
    overall = sum(totals.values())
    if overall <= 0:
        return {}
    return {speaker: total / overall for speaker, total in totals.items()}


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


def auto_noise_labels(
    shares: dict[str, float],
    embeddings: dict[str, list[float]],
    min_share: float,
    max_distance: float,
) -> set[str]:
    """Labels auto-flagged as noise: low share AND acoustically close to a
    LARGER-share speaker (echo / a real speaker's own cut voice).

    A speaker with no embedding is never flagged (cannot prove it is noise); a
    large-share speaker is never flagged no matter how close. "Larger" = strictly
    greater share, so two equal-share speakers never fold into each other.
    """
    noise: set[str] = set()
    for label, share in shares.items():
        if share >= min_share:
            continue
        emb = embeddings.get(label)
        if not emb:
            continue
        for other, other_share in shares.items():
            if other == label or other_share <= share:
                continue
            other_emb = embeddings.get(other)
            if not other_emb:
                continue
            if _cosine_distance(emb, other_emb) <= max_distance:
                noise.add(label)
                break
    return noise
```

Confirm `Any` is imported in `merge.py` (it is used throughout; `from typing import Any`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_diarization_merge.py -k "shares or auto_noise" -q -p no:warnings`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add vts/services/diarization/merge.py tests/test_diarization_merge.py
git commit -m "feat(diarization): speaker_shares + auto_noise_labels pure functions (vts-552)"
```

---

### Task 5: Write noise + share into `speaker_matches.json` (`MatchSpeakersStep`)

**Files:**
- Modify: `vts/pipeline/steps/speaker_match.py:49-80` (compute shares + auto-noise, add to each match dict)
- Test: `tests/test_speaker_match_step.py` (append)

**Interfaces:**
- Consumes: `speaker_shares`, `auto_noise_labels` (Task 4); `settings.diarization_min_speaker_share`, `settings.diarization_noise_max_distance` (Task 1).
- Produces: each entry in `speaker_matches.json` gains `"noise": bool` and `"share": float`.

- [ ] **Step 1: Write the failing test**

Read `tests/test_speaker_match_step.py` first to reuse its context/fixtures (it has `_FakeDiarizationBackend` and a helper to run the step). Append a test that runs the step on a diarization.json with a tiny echo speaker and asserts the written matches carry `noise`/`share`. Model it on the existing step-invocation test in that file; the assertion core is:

```python
    matches = json.loads((outputs_dir / "speaker_matches.json").read_text())
    assert "share" in matches["SPEAKER_00"]
    assert "noise" in matches["SPEAKER_00"]
    # tiny speaker whose embedding echoes the dominant one is auto-noise
    assert matches["SPEAKER_ECHO"]["noise"] is True
    # dominant speaker is never noise
    assert matches["SPEAKER_00"]["noise"] is False
```

Construct the `diarization.json` fixture so `SPEAKER_00` dominates (e.g. 0-100s) and `SPEAKER_ECHO` is tiny (e.g. 100-101s) with an embedding identical to `SPEAKER_00`, and a third distinct speaker to keep it realistic. Set `embeddings` in the fixture for all labels.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_speaker_match_step.py -k noise -q -p no:warnings`
Expected: FAIL — `KeyError: 'share'` (or `'noise'`)

- [ ] **Step 3: Implement in the step**

In `vts/pipeline/steps/speaker_match.py`, add imports at the top:

```python
from vts.services.diarization.merge import auto_noise_labels, speaker_shares
```

In `run`, after loading `diar` and before/around the match loop (currently the loop builds `matches` at lines 55-78), compute shares and noise once, then attach them:

```python
        diar = json.loads(diar_path.read_text(encoding="utf-8"))
        model = diar.get("embedding_model", "")
        embeddings = diar.get("embeddings", {})
        segments = diar.get("segments", []) or []

        shares = speaker_shares(segments)
        noise_labels = auto_noise_labels(
            shares,
            embeddings,
            min_share=float(getattr(ctx.settings, "diarization_min_speaker_share", 0.05)),
            max_distance=float(getattr(ctx.settings, "diarization_noise_max_distance", 0.25)),
        )
```

Then inside the `for label, vector in embeddings.items():` loop, when building `matches[label]`, add the two fields:

```python
                matches[label] = {
                    "outcome": str(outcome),
                    "speaker_id": str(nearest[0].id) if (nearest and outcome == MatchOutcome.auto) else None,
                    "distance": dist,
                    "share": shares.get(label, 0.0),
                    "noise": label in noise_labels,
                    "candidates": [
                        {"speaker_id": str(sp.id), "name": sp.name, "distance": d}
                        for sp, d in ranked
                    ],
                }
```

Note: the loop iterates `embeddings`, so a diarized speaker with no embedding gets no match row today (unchanged) — such a speaker cannot be auto-noise anyway (Task 4 guarantees it).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_speaker_match_step.py -q -p no:warnings`
Expected: PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/speaker_match.py tests/test_speaker_match_step.py
git commit -m "feat(pipeline): write noise+share into speaker_matches.json (vts-552)"
```

---

### Task 6: Disable `drop_marginal_speakers` on the live merge path

**Files:**
- Modify: `vts/pipeline/steps/transcription.py:579` (change `min_share` arg to `0.0`)
- Test: `tests/test_merge_transcript_step.py` (append) — regression for vts-0ws

**Interfaces:**
- Consumes: existing `apply_diarization` (unchanged signature).
- Produces: `merge_transcript` no longer folds low-share speakers; all diarized speakers survive in the first render.

- [ ] **Step 1: Write the failing test (vts-0ws regression)**

Append to `tests/test_merge_transcript_step.py` a test that builds task segments + a `diarization.json` where a real speaker holds ~13% of diarized time but its turns are spread across short ASR entries summing to ~3%, then runs `MergeTranscriptStep` and asserts BOTH speakers appear in the rendered `transcript.json` entries. Mirror the existing step-run helper in that file. Assertion core:

```python
    payload = json.loads((outputs_dir / "transcript.json").read_text())
    speakers = {e.get("speaker") for e in payload["entries"]}
    assert "SPEAKER_00" in speakers
    assert "SPEAKER_03" in speakers  # the 13%-real speaker must NOT be folded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_merge_transcript_step.py -k "not_folded or vts_0ws or marginal" -q -p no:warnings`
Expected: FAIL — `SPEAKER_03` absent (folded by drop_marginal_speakers)

- [ ] **Step 3: Disable the fold**

In `vts/pipeline/steps/transcription.py`, in the `apply_diarization(...)` call inside `MergeTranscriptStep.run` (currently line 579), change the `min_share` argument:

```python
                min_words=int(getattr(ctx.settings, "diarization_min_words", 2)),
                min_seconds=float(getattr(ctx.settings, "diarization_min_seconds", 0.8)),
                # Disabled on the live path (vts-552 / vts-0ws): folding low-share
                # speakers is now a reversible per-speaker "noise" flag decided at
                # match_speakers + the resolve dialog, not an irreversible merge
                # here. 0.0 makes drop_marginal_speakers a no-op (nothing is below
                # 0% share). The function + its unit tests stay for possible reuse.
                min_share=0.0,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_merge_transcript_step.py -q -p no:warnings`
Expected: PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/steps/transcription.py tests/test_merge_transcript_step.py
git commit -m "fix(pipeline): stop folding low-share speakers in merge; vts-0ws regression (vts-552)"
```

---

### Task 7: Atomic JSON write helper

**Files:**
- Modify: `vts/services/storage.py` (add `write_json_atomic` after `write_json`, ~line 35)
- Test: `tests/test_storage.py` (create if absent; else append)

**Interfaces:**
- Produces: `write_json_atomic(path: Path, payload: Any) -> None` — writes to a temp file in the same directory and `os.replace`s it into place (atomic on POSIX).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_storage.py` (create with this content if absent):

```python
import json
from pathlib import Path

from vts.services.storage import write_json_atomic


def test_write_json_atomic_roundtrip(tmp_path: Path):
    p = tmp_path / "out" / "data.json"
    write_json_atomic(p, {"a": 1, "b": [2, 3]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}


def test_write_json_atomic_no_temp_left_behind(tmp_path: Path):
    p = tmp_path / "data.json"
    write_json_atomic(p, {"x": 1})
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "data.json"]
    assert leftovers == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_storage.py -q -p no:warnings`
Expected: FAIL — `ImportError: cannot import name 'write_json_atomic'`

- [ ] **Step 3: Implement**

In `vts/services/storage.py`, add `import os` and `import tempfile` at the top if not present, then after `write_json`:

```python
def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace.

    A concurrent reader sees either the old file or the fully-written new one,
    never a torn half — needed because the transcript is now re-rendered from
    the resolve endpoint, which can overlap another save (vts-552).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=True, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_storage.py -q -p no:warnings`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add vts/services/storage.py tests/test_storage.py
git commit -m "feat(storage): write_json_atomic (vts-552)"
```

---

### Task 8: `rerender_transcript` function

**Files:**
- Create: `vts/pipeline/rerender.py`
- Test: `tests/test_rerender_transcript.py`

**Interfaces:**
- Consumes: `Repo.speaker_names_for_task`, `Repo.noise_labels_from_decisions` (Task 3); `speaker_matches.json` shape (Task 5); `render_cleaned_transcript`, `label_map`, `speaker_label_word` (existing in `merge.py`); `write_json_atomic` (Task 7).
- Produces: `async def rerender_transcript(task, session, *, language: str | None) -> None` — rewrites `<artifact>/outputs/transcript.json` and `transcript.txt` from the stored `entries`, excluding noise labels and substituting names. Idempotent. Empty-guard: if excluding noise would leave nothing, render all and log a warning.
- Also produces: `def resolve_noise_labels(matches: dict, decision_noise: set[str], has_decisions: bool) -> set[str]` (pure helper): returns `decision_noise` if `has_decisions` else labels with `matches[label].get("noise")`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rerender_transcript.py`:

```python
import json
from pathlib import Path

import pytest

from vts.pipeline.rerender import resolve_noise_labels


def test_resolve_noise_prefers_decisions_when_present():
    matches = {"A": {"noise": True}, "B": {"noise": False}}
    # decisions exist -> use them, ignore the auto suggestion in matches
    assert resolve_noise_labels(matches, {"B"}, has_decisions=True) == {"B"}


def test_resolve_noise_falls_back_to_matches_without_decisions():
    matches = {"A": {"noise": True}, "B": {"noise": False}}
    assert resolve_noise_labels(matches, set(), has_decisions=False) == {"A"}


def test_resolve_noise_empty_matches():
    assert resolve_noise_labels({}, set(), has_decisions=False) == set()
```

Add an integration test that writes a `transcript.json` with entries for `SPEAKER_00`/`SPEAKER_01`, a `speaker_matches.json` marking `SPEAKER_01` noise, and a fake `task`/`session` (use the real test DB session + a Task row, mirroring `tests/test_merge_transcript_step.py`'s setup) with a decision marking `SPEAKER_01` noise, then asserts after `rerender_transcript` the rewritten `transcript.txt` contains no `SPEAKER_01` text. Also assert:
- the rewritten `transcript.json`'s `entries` no longer contain any `SPEAKER_01` entry (this is what makes the SUMMARY exclude noise automatically — the summary reads its text and participants from `transcript.json`, spec test 5);
- **idempotency:** calling `rerender_transcript` a second time yields byte-identical `transcript.json`/`transcript.txt`;
- **empty-guard:** if ALL labels are noise, the transcript is non-empty (renders all) and a warning was logged (use `caplog`).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_rerender_transcript.py::test_resolve_noise_prefers_decisions_when_present -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'vts.pipeline.rerender'`

- [ ] **Step 3: Implement**

Create `vts/pipeline/rerender.py`:

```python
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from vts.db.repo import Repo
from vts.services.diarization.merge import (
    label_map,
    render_cleaned_transcript,
    speaker_label_word,
)
from vts.services.storage import write_json_atomic

_log = logging.getLogger(__name__)


def resolve_noise_labels(
    matches: dict[str, Any], decision_noise: set[str], has_decisions: bool
) -> set[str]:
    """Which labels are noise: the operator's decisions when any exist,
    otherwise the auto-suggestion stored in speaker_matches.json."""
    if has_decisions:
        return set(decision_noise)
    return {label for label, m in matches.items() if isinstance(m, dict) and m.get("noise")}


async def rerender_transcript(task, session, *, language: str | None) -> None:
    """Re-render transcript.json/.txt from stored entries, excluding noise
    labels and substituting registry names. Idempotent; safe on every save."""
    outputs = Path(task.artifact_dir) / "outputs"
    transcript_json = outputs / "transcript.json"
    if not transcript_json.exists():
        return
    try:
        payload = json.loads(transcript_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return

    repo = Repo(session)
    names = await repo.speaker_names_for_task(task.user_id, task.id)
    decision_noise = await repo.noise_labels_from_decisions(task.user_id, task.id)

    matches: dict[str, Any] = {}
    matches_path = outputs / "speaker_matches.json"
    if matches_path.exists():
        try:
            loaded = json.loads(matches_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                matches = loaded
        except (OSError, json.JSONDecodeError):
            matches = {}

    noise = resolve_noise_labels(matches, decision_noise, has_decisions=bool(decision_noise))

    kept = [e for e in entries if str(e.get("speaker")) not in noise]
    if not kept:
        _log.warning(
            "rerender_transcript: all speakers flagged noise for task %s; "
            "rendering all rather than an empty transcript",
            task.id,
        )
        kept = list(entries)

    mapping = label_map(kept, speaker_label_word(language), names=names)
    text = render_cleaned_transcript(kept, mapping)

    new_payload = dict(payload)
    new_payload["entries"] = kept
    new_payload["text"] = text
    write_json_atomic(transcript_json, new_payload)
    (outputs / "transcript.txt").write_text(text, encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_rerender_transcript.py -q -p no:warnings`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add vts/pipeline/rerender.py tests/test_rerender_transcript.py
git commit -m "feat(pipeline): rerender_transcript (noise-aware, idempotent) (vts-552)"
```

---

### Task 9: `can_resolve_speakers_task` capability

**Files:**
- Modify: `vts/api/main.py` (add predicate near `can_restart_summary_task`, ~line 121; add to `serialize_task`/`serialize_task_compact` capabilities dicts, ~lines 716, 781); `vts/api/schemas.py:161` (`TaskCapabilities`)
- Test: `tests/test_api_task_progress.py` or `tests/test_speaker_api.py` (append) — capability true/false by step + status

**Interfaces:**
- Consumes: `_find_step_status` (existing, `vts/api/main.py:321`), `StepStatus`, `TaskStatus`.
- Produces: `def can_resolve_speakers_task(task: Task) -> bool`; `TaskCapabilities.can_resolve_speakers: bool = False`; `capabilities["can_resolve_speakers"]` in both serializers.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speaker_api.py` (it already has task+step fixtures — read its top to match helpers):

```python
def test_can_resolve_speakers_true_after_match_speakers():
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import Task, Step, TaskStatus, StepStatus
    task = Task(status=TaskStatus.completed, options={}, source_url="u", artifact_dir="/x")
    task.steps = [Step(name="match_speakers", status=StepStatus.completed)]
    assert can_resolve_speakers_task(task) is True


def test_can_resolve_speakers_false_before_match_speakers():
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import Task, Step, TaskStatus, StepStatus
    task = Task(status=TaskStatus.running, options={}, source_url="u", artifact_dir="/x")
    task.steps = [Step(name="diarize", status=StepStatus.completed)]
    assert can_resolve_speakers_task(task) is False


def test_can_resolve_speakers_false_when_archived():
    from vts.api.main import can_resolve_speakers_task
    from vts.db.models import Task, Step, TaskStatus, StepStatus
    task = Task(status=TaskStatus.archived, options={}, source_url="u", artifact_dir="/x")
    task.steps = [Step(name="match_speakers", status=StepStatus.completed)]
    assert can_resolve_speakers_task(task) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_speaker_api.py -k can_resolve_speakers -q -p no:warnings`
Expected: FAIL — `ImportError: cannot import name 'can_resolve_speakers_task'`

- [ ] **Step 3: Implement the predicate + wire it in**

In `vts/api/main.py`, after `can_restart_final_summary_task` (~line 151):

```python
def can_resolve_speakers_task(task: Task) -> bool:
    """The voice-resolution dialog is available once match_speakers has produced
    speaker_matches.json, for the rest of the task's life except archived/canceled.
    A task-DEPENDENT capability (reads task.steps), NOT a pure-status predicate:
    the real precondition is data availability, which a status set can't express.
    """
    if task.status in {TaskStatus.archived, TaskStatus.canceled}:
        return False
    return _find_step_status(task, "match_speakers") == StepStatus.completed
```

In BOTH `serialize_task` (~line 716) and `serialize_task_compact` (~line 781) capabilities dicts, add the line:

```python
        capabilities={
            "can_restart_summary": can_restart_summary_task(task),
            "can_restart_final_summary": can_restart_final_summary_task(task),
            "can_resolve_speakers": can_resolve_speakers_task(task),
        },
```

In `vts/api/schemas.py`, `TaskCapabilities` (line 161):

```python
    can_restart_summary: bool = False
    can_restart_final_summary: bool = False
    can_resolve_speakers: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_speaker_api.py -k can_resolve_speakers -q -p no:warnings`
Expected: PASS (all three)

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py vts/api/schemas.py tests/test_speaker_api.py
git commit -m "feat(api): can_resolve_speakers capability (vts-552)"
```

---

### Task 10: Wire noise + rerender into the resolve endpoint

**Files:**
- Modify: `vts/api/schemas.py:380` (`VoiceResolution.is_noise`); `vts/api/main.py` resolve endpoint (`record_decision` call ~line 2647; capability gate; call `rerender_transcript` before continue block ~line 2660)
- Test: `tests/test_speaker_api.py` (append) — resolve/paused and resolve/completed

**Interfaces:**
- Consumes: `VoiceResolution.is_noise` (this task), `record_decision(..., is_noise=...)` (Task 2), `rerender_transcript` (Task 8), `can_resolve_speakers_task` (Task 9), `effective_language` (import from `vts.pipeline.steps.transcription`).
- Produces: resolve persists `is_noise`, re-renders the transcript, and re-queues only when `continue_task` and the task was paused.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speaker_api.py` two tests mirroring the existing resolve tests in that file (read them first for the client/fixture pattern):

- `test_resolve_persists_is_noise_and_rerenders`: POST a resolution with `is_noise=true` for a label present in a seeded `transcript.json`; assert the decision row has `is_noise=True` (query via repo) and the label's text is gone from the rewritten `transcript.txt`.
- `test_resolve_on_completed_does_not_requeue`: seed a `completed` task past `match_speakers`; POST `continue_task=false`; assert response 200, task status still `completed`, `notify_queued` not triggered (or status unchanged), transcript re-rendered.

Assertion cores:

```python
    # is_noise persisted
    labels = await repo.noise_labels_from_decisions(user_id, task_id)
    assert "SPEAKER_01" in labels
    # completed task not re-queued
    assert (await repo.get_task_by_id(task_id)).status == TaskStatus.completed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_speaker_api.py -k "is_noise or completed_does_not_requeue" -q -p no:warnings`
Expected: FAIL — `is_noise` not accepted / not persisted; or transcript unchanged.

- [ ] **Step 3: Add `is_noise` to the schema**

In `vts/api/schemas.py`, `VoiceResolution` (line 380):

```python
class VoiceResolution(BaseModel):
    speaker_label: str
    action: str  # "bind_existing" | "bind_new" | "leave_anonymous" | "accept_auto"
    speaker_id: str | None = None
    new_name: str | None = None
    add_fragment: bool = True
    distance: float | None = None
    voice_sample_id: str | None = None  # winning fragment for the decision
    outcome: str  # confirmed | rejected | manual_match | auto_accepted | auto_overridden | left_anonymous
    is_noise: bool = False
```

- [ ] **Step 4: Wire the endpoint**

In `vts/api/main.py`, add the import near the other pipeline imports at the top:

```python
from vts.pipeline.rerender import rerender_transcript
from vts.pipeline.steps.transcription import effective_language
```

In `resolve_task_speakers`, right after the `task is None` check (~line 2549), add the capability gate:

```python
        if not can_resolve_speakers_task(task):
            raise HTTPException(status_code=409, detail="cannot_resolve_speakers")
```

In the `record_decision(...)` call (~line 2647), pass `is_noise=res.is_noise`:

```python
            await repo.record_decision(
                user_id=user_id,
                source_task_id=task_id,
                speaker_label=res.speaker_label,
                speaker_id=speaker_id,
                voice_sample_id=voice_sample_id,
                distance=res.distance,
                embedding_model=embedding_model,
                outcome=res.outcome,
                is_noise=res.is_noise,
            )
```

Just before `await session.commit()` (the endpoint commits ~line 2667), re-render the transcript (needs the committed decisions visible to the same session — `record_decision` already flushed, and `rerender_transcript` reads via the same session, so call it before commit is fine; language comes from the task options + artifact dir):

```python
        language = effective_language(
            task.options if isinstance(task.options, dict) else {},
            {"outputs": Path(task.artifact_dir) / "outputs"},
        )
        await rerender_transcript(task, session, language=language)

        bus = RedisBus(redis, settings)
        if payload.continue_task:
            ...
```

Verify `effective_language`'s `dirs` argument shape — it reads `dirs["outputs"]` for the detected-language marker. Confirm:

```bash
sed -n '45,75p' vts/pipeline/steps/transcription.py
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_speaker_api.py -q -p no:warnings`
Expected: PASS (new + existing)

- [ ] **Step 6: Commit**

```bash
git add vts/api/schemas.py vts/api/main.py tests/test_speaker_api.py
git commit -m "feat(api): resolve persists is_noise, gates on capability, re-renders transcript (vts-552)"
```

---

### Task 11: `Cache-Control: no-cache` on text endpoints

**Files:**
- Modify: `vts/api/main.py` `_serve_text` (all three `Response`/`JSONResponse` returns, ~lines 601, 630, 634)
- Test: `tests/test_text_slice.py` or `tests/test_api_transcript.py` (append) — header present

**Interfaces:**
- Produces: `_serve_text` responses carry `Cache-Control: no-cache`.

- [ ] **Step 1: Write the failing test**

Find the test file that already exercises the transcript endpoint (`grep -rln "/transcript" tests/`), and append:

```python
async def test_transcript_endpoint_sends_no_cache(client, ...):
    # seed a completed task with a transcript (reuse the file's existing helper)
    r = await client.get(f"/api/tasks/{task_id}/transcript")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
```

Match the file's existing fixture/seed helper for creating a task with a transcript.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/<file> -k no_cache -q -p no:warnings`
Expected: FAIL — `cache-control` header is `None`

- [ ] **Step 3: Add the header**

In `vts/api/main.py` `_serve_text`, add `"Cache-Control": "no-cache"` to the `headers` dict of all three returns (the 206 range return ~line 601, the JSON slice return ~line 630, and the default full-text return ~line 634). Example for the default return:

```python
    # Default: full plain text, as before.
    return Response(
        content=text,
        media_type=plain_media_type,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
    )
```

Apply the same `"Cache-Control": "no-cache"` addition to the range 206 `Response` and the `JSONResponse` slice return.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/<file> -k no_cache -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vts/api/main.py tests/<file>
git commit -m "feat(api): Cache-Control no-cache on mutable text endpoints (vts-552)"
```

---

### Task 12: Frontend — noise checkbox, share, sort, buttons, capability gating

**Files:**
- Modify: `vts/static/app.js` (`buildVoiceRow` ~4170, `renderVoiceList` ~4236, `buildResolutions` ~4399, `isVoiceRowDirty` ~4217, resolve-button render ~1587, `openVoiceDialog` ~4485, `submitVoiceResolutions` ~4441)
- Modify: `vts/static/index.html` (voice row template if row markup is in HTML; else rows are built in JS — confirm)
- Modify: `vts/static/i18n/en.js`, `ru.js`, `de.js` (noise label, auto hint, share format)
- Modify: `vts/static/styles.css` (dimmed noise row)
- Test: `tests/ui/scenarios/voice-resolution-dialog.mjs` (extend) + new scenario for capability/button visibility

**Interfaces:**
- Consumes: `matches[label].noise`, `matches[label].share` (Task 5); `capabilities.can_resolve_speakers` (Task 9); `VoiceResolution.is_noise` (Task 10).
- Produces: dialog rows with a noise checkbox (pre-filled), share display, share-desc sort; button visibility driven by capability + paused; `submitVoiceResolutions` re-fetches the transcript tab.

- [ ] **Step 1: Add i18n keys (en, then ru, de)**

In `vts/static/i18n/en.js`, near the other `voices.*` keys:

```javascript
"voices.row.noise": "Noise",
"voices.row.noise_auto_hint": "auto: looks like noise/echo",
"voices.row.share": "{percent}% · {duration}",
```

`ru.js`:

```javascript
"voices.row.noise": "Шум",
"voices.row.noise_auto_hint": "авто: похоже на шум/эхо",
"voices.row.share": "{percent}% · {duration}",
```

`de.js`:

```javascript
"voices.row.noise": "Rauschen",
"voices.row.noise_auto_hint": "auto: klingt wie Rauschen/Echo",
"voices.row.share": "{percent}% · {duration}",
```

- [ ] **Step 2: Seed noise/share into row state (`buildVoiceRow`)**

In `vts/static/app.js` `buildVoiceRow`, add to the returned row object:

```javascript
    savedBinding: null,
    noise: Boolean(match.noise),
    noiseInitial: Boolean(match.noise),
    noiseAuto: Boolean(match.noise),
    share: typeof match.share === "number" ? match.share : 0,
```

- [ ] **Step 3: Sort rows by share desc in `openVoiceDialog`**

In `openVoiceDialog`, where `voiceDialogState.rows` is built (`labels.map(...)`), sort by share after mapping:

```javascript
  voiceDialogState = {
    taskId,
    paused: Boolean(paused),
    rows: labels
      .map((label) => buildVoiceRow(label, matches[label] || {}, allSpeakers))
      .sort((a, b) => b.share - a.share),
  };
```

Change `openVoiceDialog(taskId)` signature to `openVoiceDialog(taskId, paused)` and pass `paused` from the caller (the resolve-button click handler — read `runtime.baseStatus === "awaiting_input"` there).

- [ ] **Step 4: Render the noise checkbox + share + dimming (`renderVoiceList`)**

Read `renderVoiceList` (~4236) to match its row-building. For each row, append a noise checkbox and a share label; toggle a `voice-row-noise` class on the row element when checked:

```javascript
    const noiseWrap = document.createElement("label");
    noiseWrap.className = "voice-row-noise-toggle";
    const noiseBox = document.createElement("input");
    noiseBox.type = "checkbox";
    noiseBox.checked = row.noise;
    noiseBox.addEventListener("change", () => {
      row.noise = noiseBox.checked;
      rowEl.classList.toggle("voice-row-noise", row.noise);
      recomputeVoiceDirty(); // or the existing dirty refresh used in this dialog
    });
    const noiseText = document.createElement("span");
    noiseText.textContent = t("voices.row.noise");
    noiseWrap.append(noiseBox, noiseText);
    rowEl.appendChild(noiseWrap);
    rowEl.classList.toggle("voice-row-noise", row.noise);

    const shareEl = document.createElement("span");
    shareEl.className = "voice-row-share";
    shareEl.textContent = t("voices.row.share", {
      percent: Math.round(row.share * 100),
      duration: formatDuration(row.share * (voiceDialogState.totalSeconds || 0)),
    });
    rowEl.appendChild(shareEl);
    if (row.noiseAuto) {
      const hint = document.createElement("span");
      hint.className = "voice-row-noise-hint";
      hint.textContent = t("voices.row.noise_auto_hint");
      rowEl.appendChild(hint);
    }
```

Note on duration: `share` is a fraction; total task audio seconds isn't in `speaker_matches.json`. Simplest correct display is percent only if total is unknown — set `voiceDialogState.totalSeconds` from the task runtime (`runtime.stats.media_seconds`) passed into `openVoiceDialog`, or show just the percent by using `t("voices.row.share_percent_only", {percent})`. Prefer passing `mediaSeconds` into `openVoiceDialog` and computing duration from `share * mediaSeconds`; if unavailable, fall back to percent only. Add a `voices.row.share_percent_only: "{percent}%"` key to all three locales for the fallback.

- [ ] **Step 5: Include noise in dirty-tracking and payload**

In `isVoiceRowDirty` (~4217), add:

```javascript
  if (row.noise !== row.noiseInitial) return true;
```

In `buildResolutions` (~4399), add `is_noise: row.noise` to each resolution object.

- [ ] **Step 6: Gate the resolve button by capability OR paused**

In the resolve-button render (~1587):

```javascript
  if (elements.resolveVoicesBtn) {
    const paused = statusPred.needsInput(runtime.baseStatus) && runtime.awaitingStep === "match_speakers";
    const canResolve = Boolean(runtime.capabilities && runtime.capabilities.can_resolve_speakers);
    const showResolve = paused || canResolve;
    elements.resolveVoicesBtn.classList.toggle("hidden", !showResolve);
    elements.resolveVoicesBtn.disabled = !showResolve;
  }
```

- [ ] **Step 7: Toggle "Save & continue" visibility on open**

In `openVoiceDialog`, after `showModal()`, toggle the continue button:

```javascript
  if (voiceSaveContinueBtn) {
    voiceSaveContinueBtn.classList.toggle("hidden", !voiceDialogState.paused);
  }
```

- [ ] **Step 8: Re-fetch the transcript after save**

In `submitVoiceResolutions`, after `await loadTasks();` (~4474), explicitly reload the active tab's transcript for this task:

```javascript
  await loadTasks();
  const taskEl = findTaskEl(voiceTaskId); // capture taskId before closeVoiceDialog nulls state
  if (taskEl && getActiveTabName(taskEl) === "transcript") {
    await loadTabContent(taskEl, voiceTaskId, "transcript");
  }
```

Capture `const voiceTaskId = voiceDialogState.taskId;` at the top of `submitVoiceResolutions` before the dialog state is cleared.

- [ ] **Step 9: Dimmed-row CSS**

In `vts/static/styles.css`:

```css
.voice-row.voice-row-noise { opacity: 0.5; }
.voice-row-noise-hint { font-size: 0.72rem; color: var(--ink-soft); margin-left: 0.4rem; }
.voice-row-share { font-size: 0.72rem; color: var(--ink-soft); margin-left: 0.4rem; }
```

(Match the actual row class name used in `renderVoiceList`; if rows use `.voice-row` confirm via `grep -n "voice-row" vts/static/index.html vts/static/app.js`.)

- [ ] **Step 10: Extend the UI verifier scenario**

In `tests/ui/scenarios/voice-resolution-dialog.mjs`, extend the stub `speaker_matches` to include `noise`/`share` on a label, and add assertions: the noise checkbox exists and is checked for `noise:true`; rows are ordered by share desc; toggling the checkbox flips dirty state; the resolve payload carries `is_noise`. Add a second scenario file `tests/ui/scenarios/resolve-completed-capability.mjs` that stubs a `completed` task with `capabilities.can_resolve_speakers:true` and asserts the resolve button is visible and "Save & continue" is hidden; and a `running` task before match_speakers (`can_resolve_speakers:false`, not awaiting) asserts the button hidden.

- [ ] **Step 11: Run the verifier + self-check**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED` (all scenarios, including the two new/extended)

Confirm the new scenarios actually catch regressions by temporarily reverting one change (e.g. remove the noise checkbox render) and re-running the single scenario — it must FAIL — then restore.

- [ ] **Step 12: Commit**

```bash
git add vts/static/app.js vts/static/index.html vts/static/styles.css vts/static/i18n/en.js vts/static/i18n/ru.js vts/static/i18n/de.js tests/ui/scenarios/
git commit -m "feat(ui): noise checkbox, share sort, capability-gated dialog on any task (vts-552)"
```

---

### Task 13: Full suite, version bump, final commit

**Files:**
- Modify: `vts/__init__.py` (`__version__` patch bump)

- [ ] **Step 1: Run the full backend suite**

Ensure the test Postgres is up (see `tests/_db.py` / `docs/INITIAL_DEPLOYMENT.md`), then:

Run: `.venv/bin/python -m pytest -q -p no:warnings`
Expected: all pass (0 failures)

- [ ] **Step 2: Run the full UI verifier**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`

- [ ] **Step 3: Bump version**

In `vts/__init__.py`, bump the patch: `__version__ = "1.5.6"` (or next patch above the current value — check `grep __version__ vts/__init__.py` first).

- [ ] **Step 4: Commit**

```bash
git add vts/__init__.py
git commit -m "chore(release): speaker noise flag + editable bindings, version bump (vts-552)"
```

- [ ] **Step 5: Push the branch**

```bash
git push -u origin feat/speaker-noise-flag
```
