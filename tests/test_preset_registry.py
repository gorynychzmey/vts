import pytest
from vts.services.preset_registry import (
    SYSTEM_PRESETS, list_system_presets, system_preset_keys, default_system_preset,
    parse_preset_ref, preset_ref_to_dict,
)

def test_default_preset_registered():
    keys = system_preset_keys()
    assert "default" in keys
    d = default_system_preset()
    assert d.key == "default"
    assert d.display_name == "Default"
    assert d.i18n_name_key == "preset.system.default"
    assert d.options == {
        "language": None, "audio_only": False, "transcript": True,
        "prompts": [{"source": "system", "id": "summary"}],
    }

def test_parse_preset_ref_dict_and_string():
    assert parse_preset_ref({"source": "user", "id": "abc"}) == ("user", "abc")
    assert parse_preset_ref("system:default") == ("system", "default")

def test_parse_preset_ref_rejects_bad():
    with pytest.raises(ValueError):
        parse_preset_ref({"source": "nope", "id": "x"})
    with pytest.raises(ValueError):
        parse_preset_ref({"source": "user", "id": ""})

def test_ref_to_dict():
    assert preset_ref_to_dict("system", "default") == {"source": "system", "id": "default"}
