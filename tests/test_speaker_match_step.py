import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from vts.pipeline.steps.speaker_match import MatchSpeakersStep, decide_pause


def test_all_auto_no_pause():
    matches = {"S0": {"outcome": "auto"}, "S1": {"outcome": "auto"}}
    assert decide_pause(matches, no_stop=False) is False


def test_grey_pauses_when_stop_allowed():
    matches = {"S0": {"outcome": "auto"}, "S1": {"outcome": "grey"}}
    assert decide_pause(matches, no_stop=False) is True


def test_no_stop_flag_never_pauses():
    matches = {"S0": {"outcome": "miss"}, "S1": {"outcome": "grey"}}
    assert decide_pause(matches, no_stop=True) is False


# --- MatchSpeakersStep.run, stubbed repo/ctx ---------------------------------


class _FakeSpeaker:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name


class _FakeRepo:
    def __init__(self, ranked_by_label: dict) -> None:
        self._ranked_by_label = ranked_by_label
        self.calls: list[tuple] = []

    async def nearest_speakers(self, user_id, embedding, embedding_model, limit=None):
        # nearest_speakers is called once per (label, vector); we key results by
        # the vector's identity via the embedding list itself, matched by caller.
        self.calls.append((user_id, embedding, embedding_model, limit))
        return self._ranked_for(embedding)

    def _ranked_for(self, embedding):
        key = tuple(embedding)
        return self._ranked_by_label.get(key, [])


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSessionFactory:
    def __call__(self):
        return _FakeSession()


def _dirs(tmp_path: Path) -> dict[str, Path]:
    for name in ("media", "outputs", "segments", "logs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return {name: tmp_path / name for name in ("media", "outputs", "segments", "logs")}


def _state(dirs: dict[str, Path], options: dict) -> SimpleNamespace:
    import logging

    return SimpleNamespace(
        task_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
        dirs=dirs,
        logger=logging.getLogger("test"),
        task_options=options,
    )


def _ctx(repo: _FakeRepo, *, auto=0.25, candidate=0.55) -> SimpleNamespace:
    def task_flag(options, key, *, default):
        value = options.get(key, default)
        return bool(value)

    return SimpleNamespace(
        settings=SimpleNamespace(
            diarization_enabled_default=False,
            speaker_match_max_distance_auto=auto,
            speaker_match_max_distance_candidate=candidate,
        ),
        session_factory=_FakeSessionFactory(),
        task_flag=task_flag,
    )


@pytest.fixture(autouse=True)
def _patch_repo(monkeypatch):
    """MatchSpeakersStep.run constructs Repo(session) itself via `Repo(session)`.
    Patch that name in the step module so the (fake) session it's given is
    handed straight back as the repo — tests then just point the fake
    session's `_repo_override` at whichever _FakeRepo they want used."""
    import vts.pipeline.steps.speaker_match as mod

    monkeypatch.setattr(mod, "Repo", lambda session: session._repo_override)
    yield


def _wire_repo_override(ctx: SimpleNamespace, repo: _FakeRepo) -> None:
    # Sessions are created fresh per `async with`; stash the repo as a class
    # attribute so every session instance the factory hands out carries it.
    class _Session(_FakeSession):
        _repo_override = repo

    ctx.session_factory = lambda: _Session()


async def test_run_writes_speaker_matches_json_all_auto(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    write_diar = {
        "embedding_model": "ecapa",
        "embeddings": {"SPEAKER_00": [0.0, 0.0]},
    }
    (dirs["outputs"] / "diarization.json").write_text(json.dumps(write_diar), encoding="utf-8")

    sp = _FakeSpeaker(uuid.uuid4(), "Alice")
    repo = _FakeRepo({(0.0, 0.0): [(sp, 0.1)]})
    ctx = _ctx(repo)
    _wire_repo_override(ctx, repo)
    st = _state(dirs, {"diarize": True})

    result = await MatchSpeakersStep().run(ctx, st)

    assert result is True
    payload = json.loads((dirs["outputs"] / "speaker_matches.json").read_text(encoding="utf-8"))
    assert payload["SPEAKER_00"]["outcome"] == "auto"
    assert payload["SPEAKER_00"]["speaker_id"] == str(sp.id)
    assert payload["SPEAKER_00"]["distance"] == 0.1
    assert payload["SPEAKER_00"]["candidates"] == [
        {"speaker_id": str(sp.id), "name": "Alice", "distance": 0.1}
    ]


async def test_run_pauses_when_grey_and_stops_allowed(tmp_path: Path) -> None:
    from vts.pipeline.processor import TaskAwaitingInput

    dirs = _dirs(tmp_path)
    write_diar = {
        "embedding_model": "ecapa",
        "embeddings": {"SPEAKER_00": [0.0, 0.0]},
    }
    (dirs["outputs"] / "diarization.json").write_text(json.dumps(write_diar), encoding="utf-8")

    sp = _FakeSpeaker(uuid.uuid4(), "Bob")
    # distance 0.4 -> grey with default thresholds (auto<=0.25, candidate<=0.55)
    repo = _FakeRepo({(0.0, 0.0): [(sp, 0.4)]})
    ctx = _ctx(repo)
    _wire_repo_override(ctx, repo)
    st = _state(dirs, {"diarize": True})

    with pytest.raises(TaskAwaitingInput) as exc_info:
        await MatchSpeakersStep().run(ctx, st)

    assert exc_info.value.step == "match_speakers"
    payload = json.loads((dirs["outputs"] / "speaker_matches.json").read_text(encoding="utf-8"))
    assert payload["SPEAKER_00"]["outcome"] == "grey"
    assert payload["SPEAKER_00"]["speaker_id"] is None


async def test_run_no_stop_flag_skips_pause_even_with_grey(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    write_diar = {
        "embedding_model": "ecapa",
        "embeddings": {"SPEAKER_00": [0.0, 0.0]},
    }
    (dirs["outputs"] / "diarization.json").write_text(json.dumps(write_diar), encoding="utf-8")

    sp = _FakeSpeaker(uuid.uuid4(), "Bob")
    repo = _FakeRepo({(0.0, 0.0): [(sp, 0.4)]})
    ctx = _ctx(repo)
    _wire_repo_override(ctx, repo)
    st = _state(dirs, {"diarize": True, "speaker_no_manual_stop": True})

    result = await MatchSpeakersStep().run(ctx, st)

    assert result is True


async def test_run_skips_when_diarize_disabled(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    repo = _FakeRepo({})
    ctx = _ctx(repo)
    _wire_repo_override(ctx, repo)
    st = _state(dirs, {"diarize": False})

    result = await MatchSpeakersStep().run(ctx, st)

    assert result is True
    assert not (dirs["outputs"] / "speaker_matches.json").exists()


async def test_run_skips_when_no_diarization_json(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    repo = _FakeRepo({})
    ctx = _ctx(repo)
    _wire_repo_override(ctx, repo)
    st = _state(dirs, {"diarize": True})

    result = await MatchSpeakersStep().run(ctx, st)

    assert result is True
    assert not (dirs["outputs"] / "speaker_matches.json").exists()


def test_already_done_when_speaker_matches_json_exists(tmp_path: Path) -> None:
    import asyncio

    dirs = _dirs(tmp_path)
    (dirs["outputs"] / "speaker_matches.json").write_text("{}", encoding="utf-8")
    st = _state(dirs, {"diarize": True})
    ctx = _ctx(_FakeRepo({}))

    done = asyncio.run(MatchSpeakersStep().already_done(ctx, st))

    assert done is True


def test_match_speakers_is_in_the_dag_after_diarize() -> None:
    from vts.pipeline.types import DAG_HEAD

    assert "match_speakers" in DAG_HEAD
    assert DAG_HEAD.index("diarize") < DAG_HEAD.index("match_speakers")
    assert DAG_HEAD.index("match_speakers") < DAG_HEAD.index("prepare_summary_chunks")


def test_match_speakers_resolves_from_the_registry() -> None:
    from vts.pipeline.steps.registry import resolve_step

    assert isinstance(resolve_step("match_speakers"), MatchSpeakersStep)
