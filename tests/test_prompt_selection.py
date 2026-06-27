from vts.services.task_progress import selected_prompt_refs


def test_explicit_prompts_list_normalised():
    opts = {"prompts": [{"source": "system", "id": "summary"},
                        {"source": "user", "id": "abc"}]}
    assert selected_prompt_refs(opts) == [
        {"source": "system", "id": "summary"},
        {"source": "user", "id": "abc"},
    ]


def test_empty_prompts_list_stays_empty():
    assert selected_prompt_refs({"prompts": []}) == []


def test_legacy_summary_true_maps_to_summary():
    assert selected_prompt_refs({"summary": True}) == [
        {"source": "system", "id": "summary"}]


def test_legacy_summary_missing_maps_to_summary():
    assert selected_prompt_refs({}) == [{"source": "system", "id": "summary"}]


def test_legacy_summary_false_maps_to_empty():
    assert selected_prompt_refs({"summary": False}) == []


def test_malformed_entries_dropped():
    opts = {"prompts": [{"source": "bad", "id": "x"},
                        {"source": "user", "id": "ok"}]}
    assert selected_prompt_refs(opts) == [{"source": "user", "id": "ok"}]
