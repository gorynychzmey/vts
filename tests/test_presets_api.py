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
