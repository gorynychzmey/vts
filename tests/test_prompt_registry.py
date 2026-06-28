import pytest
from vts.services.prompt_registry import (
    SYSTEM_PROMPTS, list_system_prompts, system_prompt_keys,
    parse_ref, ref_to_dict, ref_key,
)


def test_summary_is_registered():
    keys = system_prompt_keys()
    assert "summary" in keys
    summary = next(p for p in list_system_prompts() if p.key == "summary")
    assert summary.file == "global_prompt.md"
    assert summary.i18n_name_key == "prompt.system.summary"
    assert summary.display_name == "Summary"


def test_parse_ref_from_dict():
    assert parse_ref({"source": "user", "id": "abc"}) == ("user", "abc")


def test_parse_ref_from_string():
    assert parse_ref("system:summary") == ("system", "summary")


def test_parse_ref_rejects_bad_source():
    with pytest.raises(ValueError):
        parse_ref({"source": "nope", "id": "x"})


def test_parse_ref_rejects_empty_id():
    with pytest.raises(ValueError):
        parse_ref({"source": "user", "id": ""})


def test_ref_helpers_roundtrip():
    assert ref_to_dict("system", "summary") == {"source": "system", "id": "summary"}
    assert ref_key("user", "abc") == "user:abc"
