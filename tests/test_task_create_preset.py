"""POST /api/tasks accepts a preset and expands it (vts-xv26).

The server-side MCP `submit_video` always took a preset; the REST endpoint did
not, and pydantic dropped the key without a word. A preset supplies the options
the caller did not set; whatever the caller set explicitly wins, even when it
equals the schema default.
"""
from __future__ import annotations

import pytest

from vts.core.config import get_settings

_HEADERS = {"X-Forwarded-User": "tester"}
_URL = "https://example.com/v"


@pytest.fixture(autouse=True)
def _artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("VTS_ARTIFACTS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "artifacts_root", tmp_path, raising=False)


class _FakeRedis:
    async def publish(self, channel, message) -> int:
        return 0

    async def get(self, key):
        return None

    async def setex(self, key, ttl, value) -> None:
        return None


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


async def _prompt(client) -> str:
    resp = await client.post(
        "/api/prompts",
        json={"name": "Memo", "system_prompt": "List the action items."},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _tag(client, name: str) -> str:
    resp = await client.post("/api/tags", json={"name": name}, headers=_HEADERS)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _preset(client, **options) -> dict:
    resp = await client.post(
        "/api/presets", json={"name": "Meeting", "options": options}, headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return {"source": "user", "id": resp.json()["id"]}


async def _submit(client, **body):
    return await client.post("/api/tasks", json={"url": _URL, **body}, headers=_HEADERS)


@pytest.mark.asyncio
async def test_a_user_preset_fills_every_option_the_caller_left_out(client):
    prompt_id = await _prompt(client)
    tag_id = await _tag(client, "Kunde A")
    preset = await _preset(
        client,
        language="de",
        diarize=True,
        prompts=[{"source": "user", "id": prompt_id}],
        tags=[tag_id],
    )

    resp = await _submit(client, preset=preset)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["options"]["language"] == "de"
    assert body["options"]["diarize"] is True
    assert body["options"]["prompts"] == [{"source": "user", "id": prompt_id}]
    assert [t["name"] for t in body["tags"]] == ["Kunde A"]
    # The ref is a submit-time instruction, not an option the worker reads.
    assert "preset" not in body["options"]


@pytest.mark.asyncio
async def test_an_explicit_value_wins_even_when_it_equals_the_default(client):
    """`diarize: false` is the schema default. Sent explicitly, it is still the
    caller's decision and must beat a preset that diarizes."""
    preset = await _preset(client, language="de", diarize=True)

    resp = await _submit(client, preset=preset, diarize=False, language="ru", tags=[])

    assert resp.status_code == 200, resp.text
    options = resp.json()["options"]
    assert options["diarize"] is False
    assert options["language"] == "ru"
    # Untouched by the caller, so still the preset's prompts (none were set:
    # PresetOptions defaults to an empty list).
    assert options["prompts"] == []


@pytest.mark.asyncio
async def test_the_system_default_preset_gives_the_summary_prompt(client):
    resp = await _submit(client, preset={"source": "system", "id": "default"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["options"]["prompts"] == [{"source": "system", "id": "summary"}]


@pytest.mark.asyncio
async def test_stage_rules_are_checked_after_the_preset_is_applied(client):
    """The preset turns the transcript off; the caller asks for a prompt. The
    schema validator saw transcript=True (its default) and let it through, so
    the merged options must be validated again."""
    preset = await _preset(client, transcript=False, prompts=[])

    resp = await _submit(client, preset=preset, prompts=[{"source": "system", "id": "summary"}])

    assert resp.status_code == 422, resp.text
    assert "prompts require transcript" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preset", "status"),
    [
        ({"source": "user", "id": "00000000-0000-0000-0000-000000000000"}, 404),
        ({"source": "system", "id": "no-such-preset"}, 404),
        ({"source": "nobody", "id": "x"}, 422),
    ],
)
async def test_an_unknown_or_malformed_preset_is_refused(client, preset, status):
    resp = await _submit(client, preset=preset)

    assert resp.status_code == status, resp.text


@pytest.mark.asyncio
async def test_without_a_preset_nothing_changes(client):
    resp = await _submit(client)

    assert resp.status_code == 200, resp.text
    options = resp.json()["options"]
    assert options["prompts"] == [{"source": "system", "id": "summary"}]
    assert options["diarize"] is False
    assert "preset" not in options
