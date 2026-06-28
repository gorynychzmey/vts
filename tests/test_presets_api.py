import pytest
from pydantic import ValidationError
from vts.api.schemas import PresetRef, PresetOptions, PresetCreateRequest, PresetUpdateRequest

def test_preset_options_defaults():
    o = PresetOptions()
    assert o.language is None and o.audio_only is False and o.transcript is True and o.prompts == []

def test_preset_create_validates():
    with pytest.raises(ValidationError):
        PresetCreateRequest(name="", options=PresetOptions())

def test_preset_update_blank_name_rejected():
    with pytest.raises(ValidationError):
        PresetUpdateRequest(name="   ")


@pytest.mark.asyncio
async def test_presets_list_includes_system_default(client):
    body = (await client.get("/api/presets")).json()
    sys = next(p for p in body if p["source"]=="system" and p["id"]=="default")
    assert sys["editable"] is False
    assert sys["options"]["transcript"] is True
    assert sys["options"]["prompts"] == [{"source":"system","id":"summary"}]

@pytest.mark.asyncio
async def test_preset_crud_and_default_endpoints(client):
    created = (await client.post("/api/presets", json={
        "name":"Mine","options":{"language":"en","audio_only":True,"transcript":True,
        "prompts":[{"source":"system","id":"summary"}]}})).json()
    pid = created["id"]; assert created["source"]=="user" and created["editable"] is True
    # default starts as system
    assert (await client.get("/api/me/default_preset")).json() == {"source":"system","id":"default"}
    # set user default
    assert (await client.put("/api/me/default_preset", json={"source":"user","id":pid})).status_code == 204
    assert (await client.get("/api/me/default_preset")).json() == {"source":"user","id":pid}
    # set unknown user default -> 404
    import uuid
    assert (await client.put("/api/me/default_preset", json={"source":"user","id":str(uuid.uuid4())})).status_code == 404
    # delete the default preset -> default falls back to system
    assert (await client.delete(f"/api/presets/{pid}")).status_code == 204
    assert (await client.get("/api/me/default_preset")).json() == {"source":"system","id":"default"}
