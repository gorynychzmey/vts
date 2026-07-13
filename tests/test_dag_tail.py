from vts.pipeline.types import DAG_HEAD, finalize_step_name, build_dag_steps


def test_summary_keeps_legacy_step_name():
    assert finalize_step_name("system", "summary") == "summarize_final"


def test_custom_prompt_step_name():
    assert finalize_step_name("user", "abc") == "finalize:user:abc"


def test_build_dag_summary_only():
    steps = build_dag_steps({"prompts": [{"source": "system", "id": "summary"}]})
    assert steps[-1] == "summarize_final"
    assert "pack_window_notes" in steps


def test_build_dag_summary_plus_custom():
    steps = build_dag_steps({"prompts": [
        {"source": "system", "id": "summary"},
        {"source": "user", "id": "abc"},
    ]})
    assert steps[-2:] == ["summarize_final", "finalize:user:abc"]


def test_build_dag_no_prompts_has_no_finalize():
    steps = build_dag_steps({"prompts": []})
    assert not any(s.startswith("finalize:") for s in steps)
    assert "summarize_final" not in steps


def test_lane_for_step_mapping():
    from vts.pipeline.types import lane_for_step
    assert lane_for_step("download") == "network"
    for s in ("extract_audio", "trim_initial_silence", "segment_audio"):
        assert lane_for_step(s) == "ffmpeg"
    for s in ("detect_language", "transcribe_segments", "prepare_llama_model",
              "summarize_windows", "pack_window_notes", "summarize_final",
              "merge_transcript", "prepare_summary_chunks", "finalize:user:abc"):
        assert lane_for_step(s) is None
