"""The MCP Apps speakers view (vts-vtma.6).

Two things are pinned here. The mapping from a view's plain choice to the
resolution the save service takes must match the web dialog's
buildResolutions / resolveOutcomeCode (vts/static/app.js) row for row — the
outcomes feed matcher calibration, so two UIs writing different codes for the
same choice would corrupt it. And the tools work end to end against the real
database and files: show the voices, save a choice, serve a clip only through
its own signed link.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest
from fastmcp import Client

from tests.conftest import _TEST_USER_ID
from vts.core.config import Settings
from vts.mcp.tools_registry.apps import resolution_for_choice

_OWNER = uuid.UUID(_TEST_USER_ID)
A, B, C = (str(uuid.uuid4()) for _ in range(3))
PEOPLE = [{"id": A, "name": "Anna"}, {"id": B, "name": "Boris"}, {"id": C, "name": "Clara"}]


def _match(outcome, matched=None, candidates=()):
    return {"outcome": outcome, "speaker_id": matched, "distance": 0.3 if matched else None,
            "candidates": [{"speaker_id": sid, "name": n, "distance": d} for sid, n, d in candidates]}


# ------------------------------------------------------------ parity with the web dialog

@pytest.mark.parametrize("outcome, matched, choice, action, code", [
    # miss: nobody matched
    ("miss", None, {"kind": "person", "speaker_id": A}, "bind_existing", "manual_match"),
    ("miss", None, {"kind": "new", "new_name": "Dora"}, "bind_new", "left_anonymous"),
    ("miss", None, {"kind": "anonymous"}, "leave_anonymous", "left_anonymous"),
    # auto: the matcher bound A on its own
    ("auto", A, {"kind": "person", "speaker_id": A}, "accept_auto", "auto_accepted"),
    ("auto", A, {"kind": "person", "speaker_id": B}, "bind_existing", "auto_overridden"),
    ("auto", A, {"kind": "anonymous"}, "leave_anonymous", "auto_overridden"),
    ("auto", A, {"kind": "new", "new_name": "Dora"}, "bind_new", "auto_overridden"),
    # grey: a suggestion, B ranked first
    ("grey", None, {"kind": "person", "speaker_id": B}, "bind_existing", "confirmed"),
    ("grey", None, {"kind": "person", "speaker_id": A}, "bind_existing", "rejected"),
    ("grey", None, {"kind": "anonymous"}, "leave_anonymous", "left_anonymous"),
])
def test_choices_map_like_the_web_dialog(outcome, matched, choice, action, code):
    candidates = [(B, "Boris", 0.2), (A, "Anna", 0.4)] if outcome != "miss" else []
    res = resolution_for_choice("SPEAKER_00", _match(outcome, matched, candidates), choice, PEOPLE)
    assert (res["action"], res["outcome"]) == (action, code)


def test_a_new_person_without_a_name_is_left_anonymous():
    # The web dialog does the same with an empty "add new" field.
    res = resolution_for_choice("SPEAKER_00", _match("miss"), {"kind": "new", "new_name": "  "}, PEOPLE)
    assert res["action"] == "leave_anonymous" and res["add_fragment"] is False


def test_fragment_default_and_distance_follow_the_web_dialog():
    m = _match("grey", None, [(B, "Boris", 0.2), (A, "Anna", 0.4)])
    res = resolution_for_choice("SPEAKER_00", m, {"kind": "person", "speaker_id": A}, PEOPLE)
    # Remember the voice by default for grey/auto, with the chosen person's distance.
    assert res["add_fragment"] is True and res["distance"] == 0.4
    miss = resolution_for_choice("SPEAKER_00", _match("miss"), {"kind": "person", "speaker_id": C}, PEOPLE)
    # A person picked for an unmatched voice has no distance to report.
    assert miss["add_fragment"] is False and miss["distance"] is None
    noise = resolution_for_choice("SPEAKER_00", m, {"kind": "anonymous", "is_noise": True}, PEOPLE)
    assert noise["is_noise"] is True


@pytest.mark.parametrize("choice", [{"kind": "person"}, {"kind": "telepathy"}])
def test_malformed_choices_are_refused(choice):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        resolution_for_choice("SPEAKER_00", _match("miss"), choice, PEOPLE)


# ------------------------------------------------------------ end to end

@dataclass
class _User:
    id: str = _TEST_USER_ID
    username: str = "tester"


class _FakeRedis:
    async def publish(self, channel, message):
        return 0

    async def delete(self, key):
        return 0

    async def aclose(self):
        pass


class _FakeDiarization:
    def __init__(self):
        self.embed_calls = []

    async def embed(self, path):
        self.embed_calls.append(str(path))
        return [0.2] * 256


async def _seed(factory, tmp_path, *, detach=False):
    """A diarized task awaiting input, two voices with one preview clip each,
    and its recording."""
    from vts.db.models import StepStatus, TaskStatus
    from vts.db.repo import Repo

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "diarization.json").write_text(json.dumps({"embedding_model": "ecapa"}), encoding="utf-8")
    (outputs / "speaker_matches.json").write_text(json.dumps({
        "SPEAKER_00": {"outcome": "miss", "speaker_id": None, "candidates": []},
        "SPEAKER_01": {"outcome": "miss", "speaker_id": None, "candidates": []},
    }), encoding="utf-8")
    previews = {}
    for label in ("SPEAKER_00", "SPEAKER_01"):
        clip = outputs / f"preview_{label}_0.wav"
        clip.write_bytes(f"RIFF-{label}".encode())
        previews[label] = [{"path": str(clip), "start": 0.0, "end": 3.0}]
    (outputs / "speaker_previews.json").write_text(json.dumps(previews), encoding="utf-8")
    async with factory() as s:
        repo = Repo(s)
        task = await repo.create_task(user_id=_OWNER, source_url="https://example.com/v",
                                      options={"diarize": True}, artifact_dir=str(tmp_path))
        step = await repo.upsert_step(task.id, "match_speakers")
        await repo.set_step_status(step, StepStatus.completed)
        await repo.set_task_status(task, TaskStatus.awaiting_input)
        recording = await repo.upsert_recording_for_task(task)
        if detach:
            recording.source_task_id = None
        await s.commit()
        return task.id, recording.id


@pytest.fixture
def mcp_for(authed_app, monkeypatch):
    import vts.mcp.tools_registry.apps as apps_mod
    import vts.services.diarization as diarization_mod
    import vts.services.redis_client as redis_client
    from vts.mcp.server import build_mcp_server

    _app, factory = authed_app
    diar = _FakeDiarization()
    settings = Settings(public_base_url="https://vts.example.org", session_secret="test-session-secret")

    async def _auth(_session):
        return _User(), settings

    monkeypatch.setattr(apps_mod, "mcp_authenticate", _auth)
    monkeypatch.setattr(apps_mod, "get_db_session_factory", lambda: factory)
    monkeypatch.setattr(redis_client, "make_redis", lambda _url: _FakeRedis())
    monkeypatch.setattr(diarization_mod, "create_diarization_backend", lambda *_a, **_k: diar)
    c = Client(build_mcp_server())
    c.diar = diar
    c.settings = settings
    return c, factory


async def test_show_speakers_lists_the_voices(mcp_for, tmp_path):
    client, factory = mcp_for
    _task, rec = await _seed(factory, tmp_path)
    async with client:
        data = (await client.call_tool("show_speakers", {"recording_id": str(rec)})).structured_content
    assert data["editable"] is True and data["awaiting_input"] is True
    assert [v["label"] for v in data["voices"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert all(v["clips"] == 1 for v in data["voices"])
    assert all(v["decided_speaker_id"] is None for v in data["voices"])


async def test_saving_a_new_person_goes_through_the_shared_service(mcp_for, tmp_path):
    client, factory = mcp_for
    task_id, rec = await _seed(factory, tmp_path)
    async with client:
        saved = await client.call_tool("app_resolve_speakers", {
            "recording_id": str(rec),
            "voices": {"SPEAKER_00": {"kind": "new", "new_name": "Вася"},
                       "SPEAKER_01": {"kind": "anonymous", "is_noise": True}},
        })
        assert saved.structured_content["results"] == {"SPEAKER_00": "resolved", "SPEAKER_01": "resolved"}
        data = (await client.call_tool("show_speakers", {"recording_id": str(rec)})).structured_content
    by = {v["label"]: v for v in data["voices"]}
    assert by["SPEAKER_00"]["decided_name"] == "Вася"
    assert by["SPEAKER_01"]["decided_is_noise"] is True
    # A new person gets the voice's fragment, embedded like in the web dialog.
    assert client.diar.embed_calls and client.diar.embed_calls[0].endswith("preview_SPEAKER_00_0.wav")
    from vts.db.repo import Repo

    async with factory() as s:
        decisions = await Repo(s).decisions_for_task(_OWNER, task_id)
    assert decisions["SPEAKER_00"]["name"] == "Вася"


async def test_unknown_voices_and_lost_tasks_are_refused(mcp_for, tmp_path):
    client, factory = mcp_for
    _task, rec = await _seed(factory, tmp_path / "a")
    _t2, detached = await _seed(factory, tmp_path / "b", detach=True)
    async with client:
        bad = await client.call_tool("app_resolve_speakers", {
            "recording_id": str(rec), "voices": {"SPEAKER_99": {"kind": "anonymous"}}}, raise_on_error=False)
        assert bad.is_error and "SPEAKER_99" in bad.content[0].text
        lost = await client.call_tool("app_resolve_speakers", {
            "recording_id": str(detached), "voices": {"SPEAKER_00": {"kind": "anonymous"}}}, raise_on_error=False)
        assert lost.is_error and "deleted" in lost.content[0].text
        view = (await client.call_tool("show_speakers", {"recording_id": str(detached)})).structured_content
    assert view["editable"] is False and view["reason"] == "task_deleted"


async def test_a_clip_link_opens_that_clip_and_nothing_else(mcp_for, tmp_path, authed_app, client):
    mcp, factory = mcp_for
    app, _f = authed_app
    from vts.api.deps import get_settings_dep

    app.dependency_overrides[get_settings_dep] = lambda: mcp.settings
    _task, rec = await _seed(factory, tmp_path)
    async with mcp:
        link = (await mcp.call_tool("app_speaker_preview_url", {
            "recording_id": str(rec), "label": "SPEAKER_01"})).structured_content
    url = link["url"]
    assert url.startswith(f"https://vts.example.org/api/recordings/{rec}/speaker-previews/SPEAKER_01/0/audio?")
    path = url[len("https://vts.example.org"):]
    r = await client.get(path)
    assert r.status_code == 200 and r.content == b"RIFF-SPEAKER_01"
    # The same signature on another voice's clip, or on the media, opens nothing.
    other = path.replace("SPEAKER_01", "SPEAKER_00")
    assert (await client.get(other)).status_code == 404
    query = path.split("?", 1)[1]
    assert (await client.get(f"/api/recordings/{rec}/media?{query}")).status_code in (404, 422)
