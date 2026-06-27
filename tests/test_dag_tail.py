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
