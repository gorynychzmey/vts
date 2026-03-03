"""Unit tests for vts.metrics."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from vts.metrics.aggregation import (
    aggregate_task_metrics,
    compute_percentile,
    compute_worst_n,
)
from vts.metrics.emitter import MetricsEmitter
from vts.metrics.quality import (
    QualityAnalyzer,
    compute_redundancy,
    extract_dates,
    extract_numbers,
    extract_units,
    format_metrics,
    hamming_distance,
    simhash,
    split_sentences,
)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

class TestExtractNumbers:
    def test_integer(self):
        assert "42" in extract_numbers("There are 42 items")

    def test_negative(self):
        assert "-5" in extract_numbers("Temperature is -5 degrees")

    def test_thousands_space(self):
        # "1 000" → "1000"
        nums = extract_numbers("Total: 1 000 units")
        assert any("1000" in n or "1" in n for n in nums)

    def test_no_false_positives(self):
        nums = extract_numbers("No numbers here.")
        assert len(nums) == 0

    def test_decimal(self):
        nums = extract_numbers("Result is 3.14")
        assert any("3" in n for n in nums)

    def test_mismatch_count(self):
        transcript = "We shipped 100 boxes and earned 5000 euros"
        summary = "We shipped 100 boxes and earned 9999 euros"
        t = extract_numbers(transcript)
        s = extract_numbers(summary)
        mismatch = s - t
        assert len(mismatch) >= 1
        assert "9999" in mismatch or any("9999" in m for m in mismatch)

    def test_no_mismatch_same_numbers(self):
        text = "Revenue: 1234 and cost: 567"
        t = extract_numbers(text)
        s = extract_numbers(text)
        assert len(s - t) == 0


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

class TestExtractDates:
    def test_iso(self):
        dates = extract_dates("Event on 2024-03-15")
        assert "2024-03-15" in dates

    def test_dot_format(self):
        dates = extract_dates("Meeting on 15.03.2024")
        assert "15.03.2024" in dates

    def test_slash_format(self):
        dates = extract_dates("Date: 15/03/2024")
        assert "15/03/2024" in dates

    def test_named_month_en(self):
        dates = extract_dates("The conference is on 15 March")
        assert len(dates) >= 1

    def test_named_month_ru(self):
        dates = extract_dates("Конференция 15 марта")
        assert len(dates) >= 1

    def test_mismatch(self):
        transcript = "On 2024-01-01 we launched"
        summary = "On 2025-12-31 we launched"
        t = extract_dates(transcript)
        s = extract_dates(summary)
        assert len(s - t) >= 1

    def test_no_dates(self):
        dates = extract_dates("Nothing temporal here.")
        assert len(dates) == 0


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

class TestExtractUnits:
    def test_milliseconds(self):
        units = extract_units("Latency is 150ms")
        assert len(units) >= 1
        assert any("150" in u for u in units)

    def test_percentage(self):
        units = extract_units("CPU usage: 85%")
        assert any("85" in u and "%" in u for u in units)

    def test_megabytes(self):
        units = extract_units("File size: 256mb")
        assert len(units) >= 1

    def test_mismatch(self):
        transcript = "Processed 50gb of data"
        summary = "Processed 100tb of data"
        t = extract_units(transcript)
        s = extract_units(summary)
        mismatch = s - t
        assert len(mismatch) >= 1

    def test_no_units(self):
        units = extract_units("No measurements here.")
        assert len(units) == 0


# ---------------------------------------------------------------------------
# Redundancy (SimHash)
# ---------------------------------------------------------------------------

class TestSimHash:
    def test_identical_texts(self):
        text = "The quick brown fox"
        h1 = simhash(text)
        h2 = simhash(text)
        assert h1 == h2

    def test_different_texts(self):
        h1 = simhash("The quick brown fox jumps over the lazy dog")
        h2 = simhash("Completely different sentence about cats and rain")
        assert h1 != h2

    def test_hamming_zero_for_equal(self):
        h = simhash("Hello world")
        assert hamming_distance(h, h) == 0

    def test_hamming_max_for_complement(self):
        assert hamming_distance(0, (1 << 64) - 1) == 64


class TestRedundancy:
    def test_no_duplicates(self):
        text = "First sentence here. Second very different sentence. Third completely new idea."
        ratio = compute_redundancy(text)
        assert ratio == 0.0

    def test_exact_duplicates(self):
        sent = "The project was a success and everyone was happy."
        text = f"{sent} {sent} {sent}"
        ratio = compute_redundancy(text, max_hamming=0)
        assert ratio > 0.0

    def test_single_sentence(self):
        ratio = compute_redundancy("Only one sentence here.")
        assert ratio == 0.0

    def test_empty_text(self):
        ratio = compute_redundancy("")
        assert ratio == 0.0


# ---------------------------------------------------------------------------
# Format metrics
# ---------------------------------------------------------------------------

class TestFormatMetrics:
    def test_bullet_ratio(self):
        text = "- Item one\n- Item two\n- Item three\nNormal line"
        result = format_metrics(text)
        assert result["bullet_ratio"] > 0.5

    def test_heading_count(self):
        text = "# Heading\n\nSome paragraph.\n\n## Sub-heading\n\nMore text."
        result = format_metrics(text)
        assert result["heading_count"] >= 2

    def test_paragraph_count(self):
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        result = format_metrics(text)
        assert result["paragraph_count"] == 3

    def test_empty_text(self):
        result = format_metrics("")
        assert result["paragraph_count"] == 0
        assert result["bullet_ratio"] == 0.0
        assert result["heading_count"] == 0

    def test_no_bullets(self):
        text = "Regular prose without bullets."
        result = format_metrics(text)
        assert result["bullet_ratio"] == 0.0


# ---------------------------------------------------------------------------
# QualityAnalyzer
# ---------------------------------------------------------------------------

class TestQualityAnalyzer:
    def setup_method(self):
        self.qa = QualityAnalyzer()

    def test_compression_ratio_exact_tokens(self):
        result = self.qa.analyze(
            summary_text="Short summary.",
            transcript_text="Much longer transcript text with many more words.",
            prompt_tokens=50,
            completion_tokens=10,
        )
        assert result["compression_ratio"] == pytest.approx(0.2, abs=0.01)
        assert result["token_estimate"] is False

    def test_compression_ratio_estimate(self):
        transcript = "a " * 100  # ~100 chars → ~25 tokens
        summary = "a " * 50  # ~50 chars → ~12 tokens
        result = self.qa.analyze(summary_text=summary, transcript_text=transcript)
        assert result["token_estimate"] is True
        assert 0.0 < result["compression_ratio"] < 1.0

    def test_number_mismatch(self):
        transcript = "We sold 100 units at 5 dollars each."
        summary = "We sold 999 units at 5 dollars each."
        result = self.qa.analyze(
            summary_text=summary, transcript_text=transcript,
            prompt_tokens=20, completion_tokens=15,
        )
        assert result["number_mismatch_count"] >= 1

    def test_no_number_mismatch(self):
        text = "Revenue was 42 million."
        result = self.qa.analyze(
            summary_text=text, transcript_text=text,
            prompt_tokens=10, completion_tokens=10,
        )
        assert result["number_mismatch_count"] == 0

    def test_format_field_present(self):
        result = self.qa.analyze(
            summary_text="- point one\n- point two\n",
            transcript_text="We discussed two points.",
            prompt_tokens=10, completion_tokens=8,
        )
        assert "format" in result
        assert result["format"]["bullet_ratio"] > 0


# ---------------------------------------------------------------------------
# MetricsEmitter (JSONL)
# ---------------------------------------------------------------------------

class TestMetricsEmitter:
    def test_emit_writes_valid_jsonl(self, tmp_path):
        path = tmp_path / "metrics.jsonl"
        em = MetricsEmitter(task_id="task-1", run_id="run-1", jsonl_path=path)
        em.emit({"stage": "download", "status": "ok", "t_wall_ms": 500})
        em.emit({"stage": "transcribe.segment", "status": "ok", "t_wall_ms": 1200})

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)  # must be valid JSON
            assert "ts" in obj
            assert "task_id" in obj
            assert "run_id" in obj

    def test_emit_one_line_per_event(self, tmp_path):
        path = tmp_path / "metrics.jsonl"
        em = MetricsEmitter(task_id="t", run_id="r", jsonl_path=path)
        for i in range(5):
            em.emit({"stage": f"step_{i}", "status": "ok"})
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_disabled_emitter_writes_nothing(self, tmp_path):
        path = tmp_path / "metrics.jsonl"
        em = MetricsEmitter(task_id="t", run_id="r", jsonl_path=path, enabled=False)
        em.emit({"stage": "download", "status": "ok"})
        assert not path.exists()

    def test_no_path_no_crash(self):
        em = MetricsEmitter(task_id="t", run_id="r", jsonl_path=None)
        em.emit({"stage": "download", "status": "ok"})
        assert len(em.all_events()) == 1

    def test_all_events_accumulates(self, tmp_path):
        em = MetricsEmitter(task_id="t", run_id="r", jsonl_path=tmp_path / "m.jsonl")
        em.emit({"stage": "a", "status": "ok"})
        em.emit({"stage": "b", "status": "ok"})
        events = em.all_events()
        assert len(events) == 2
        assert events[0]["stage"] == "a"
        assert events[1]["stage"] == "b"

    def test_task_id_and_run_id_in_every_event(self, tmp_path):
        path = tmp_path / "m.jsonl"
        em = MetricsEmitter(task_id="my-task", run_id="my-run", jsonl_path=path)
        em.emit({"stage": "test", "status": "ok"})
        obj = json.loads(path.read_text().strip())
        assert obj["task_id"] == "my-task"
        assert obj["run_id"] == "my-run"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_compute_percentile_single(self):
        assert compute_percentile([5.0], 50) == pytest.approx(5.0)

    def test_compute_percentile_median(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert compute_percentile(vals, 50) == pytest.approx(3.0)

    def test_compute_percentile_p95(self):
        vals = list(range(1, 21))  # 1..20
        p95 = compute_percentile([float(v) for v in vals], 95)
        assert p95 >= 19.0

    def test_compute_worst_n(self):
        events = [
            {"stage": "summarize.segment", "segment_id": 1, "number_mismatch_count": 3},
            {"stage": "summarize.segment", "segment_id": 2, "number_mismatch_count": 7},
            {"stage": "summarize.segment", "segment_id": 3, "number_mismatch_count": 1},
            {"stage": "summarize.segment", "segment_id": 4, "number_mismatch_count": 5},
        ]
        worst = compute_worst_n(events, "number_mismatch_count", 2)
        assert len(worst) == 2
        assert worst[0]["number_mismatch_count"] == 7
        assert worst[1]["number_mismatch_count"] == 5

    def test_aggregate_task_metrics(self):
        events = [
            {
                "stage": "transcribe.segment", "segment_id": 1,
                "t_wall_ms": 5000, "rtf": 0.15, "status": "ok",
            },
            {
                "stage": "transcribe.segment", "segment_id": 2,
                "t_wall_ms": 6000, "rtf": 0.22, "status": "ok",
            },
            {
                "stage": "summarize.segment", "segment_id": 1,
                "t_wall_ms": 3000, "llm_tok_per_s": 14.5,
                "compression_ratio": 0.38, "redundancy_dup_sentence_ratio": 0.05,
                "number_mismatch_count": 1, "status": "ok",
            },
            {
                "stage": "summarize.global",
                "t_wall_ms": 8000, "llm_tok_per_s": 12.0,
                "compression_ratio": 0.45, "redundancy_dup_sentence_ratio": 0.02,
                "number_mismatch_count": 0, "status": "ok",
            },
        ]
        result = aggregate_task_metrics(events)
        assert result["p50_rtf"] is not None
        assert result["p95_rtf"] is not None
        assert result["p50_compression_ratio"] is not None
        assert result["total_wall_ms_by_stage"]["transcribe.segment"] == 11000
        assert len(result["worst3_number_mismatch"]) <= 3
