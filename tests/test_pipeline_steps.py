from vts.pipeline.types import DAG_STEPS


def test_prepare_summary_chunks_is_between_warmup_and_window_summary() -> None:
    warmup_idx = DAG_STEPS.index("prepare_llama_model")
    chunk_idx = DAG_STEPS.index("prepare_summary_chunks")
    windows_idx = DAG_STEPS.index("summarize_windows")
    assert warmup_idx < chunk_idx < windows_idx
