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


from vts.services.preset_expand import filter_prompt_refs, expand_preset_options, resolve_preset

def test_resolve_preset_system_default_returns_registry_options():
    out = resolve_preset("system", "default", list_system_presets(), None)
    assert out == default_system_preset().options

def test_resolve_preset_unknown_system_returns_none():
    assert resolve_preset("system", "nope", list_system_presets(), None) is None

def test_resolve_preset_user_returns_passed_options():
    opts = {"language": "ru", "prompts": []}
    assert resolve_preset("user", "abc", list_system_presets(), opts) == opts
    assert resolve_preset("user", "abc", list_system_presets(), None) is None

def test_filter_keeps_system_drops_unknown_user():
    refs = [{"source":"system","id":"summary"},
            {"source":"user","id":"keep"},
            {"source":"user","id":"gone"}]
    assert filter_prompt_refs(refs, {"keep"}) == [
        {"source":"system","id":"summary"}, {"source":"user","id":"keep"}]

def test_expand_defaults_missing_and_filters():
    opts = {"audio_only": True, "prompts": [{"source":"user","id":"gone"}]}
    out = expand_preset_options(opts, set())
    assert out == {"language": None, "audio_only": True, "transcript": True, "diarize": False, "prompts": []}

def test_expand_diarize_defaults_false_when_missing():
    out = expand_preset_options({}, set())
    assert out["diarize"] is False

def test_expand_diarize_true_survives():
    # A preset saved with diarize=True must not be silently dropped by
    # expand_preset_options's explicit-key rebuild.
    opts = {"transcript": True, "diarize": True}
    out = expand_preset_options(opts, set())
    assert out["diarize"] is True
