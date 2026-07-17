import pytest

from vts.services.diarization import create_diarization_backend
from vts.services.diarization._pyannote import PyannoteBackend


def _pyannote() -> PyannoteBackend:
    return PyannoteBackend("http://localhost")


def test_create_backend_returns_pyannote() -> None:
    backend = create_diarization_backend("http://localhost", "pyannote")
    assert isinstance(backend, PyannoteBackend)
    assert backend.backend_name == "pyannote"


def test_create_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown diarization backend"):
        create_diarization_backend("http://localhost", "nope")


def test_normalize_output_extracts_segments_and_embeddings() -> None:
    payload = {
        "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
        "embeddings": {"SPEAKER_00": [0.1, 0.2]},
        "num_speakers": 1,
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    assert result["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}
    assert result["num_speakers"] == 1


def test_normalize_output_defaults_when_fields_missing() -> None:
    result = _pyannote().normalize_output({})
    assert result == {"segments": [], "embeddings": {}, "num_speakers": 0}


def test_normalize_output_drops_malformed_segments() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "speaker": "SPEAKER_01"},
            "garbage",
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]


def test_normalize_output_infers_num_speakers() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
        ]
    }
    assert _pyannote().normalize_output(payload)["num_speakers"] == 2


def test_normalize_output_infers_num_speakers_counts_distinct() -> None:
    # 3 segments but only 2 distinct speakers: pins distinct-speaker-count
    # semantics against a segment-count mutation (len(segments) would give 3).
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
            {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_00"},
        ]
    }
    assert _pyannote().normalize_output(payload)["num_speakers"] == 2


def test_normalize_output_segments_not_a_list_is_treated_as_empty() -> None:
    # The sidecar is untrusted input; a wrong type must not be iterated.
    result = _pyannote().normalize_output({"segments": 5})
    assert result["segments"] == []
    assert result["num_speakers"] == 0


def test_normalize_output_segments_as_dict_is_treated_as_empty() -> None:
    result = _pyannote().normalize_output({"segments": {"start": 0.0}})
    assert result["segments"] == []


def test_normalize_output_segments_as_string_is_treated_as_empty() -> None:
    result = _pyannote().normalize_output({"segments": "garbage"})
    assert result["segments"] == []


def test_normalize_output_drops_segment_with_non_numeric_start() -> None:
    payload = {
        "segments": [
            {"start": "abc", "end": "5.0", "speaker": "SPEAKER_00"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == []


def test_normalize_output_drops_segment_with_non_numeric_end() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": "not-a-number", "speaker": "SPEAKER_00"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == []


def test_normalize_output_keeps_good_segments_alongside_dropped_ones() -> None:
    # The actual promise: one bad segment must not take good ones down with it.
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": "abc", "end": "5.0", "speaker": "SPEAKER_01"},
            {"start": 5.0, "end": "bad", "speaker": "SPEAKER_02"},
            {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_03"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_03"},
    ]


def test_normalize_output_warns_when_every_segment_is_dropped(caplog) -> None:
    # Dropping is silent by design, so a systematically broken sidecar would
    # look like a quiet monologue. The log is what makes it visible.
    payload = {"segments": [{"start": "abc", "end": "x", "speaker": "SPEAKER_00"}]}
    with caplog.at_level("WARNING"):
        result = _pyannote().normalize_output(payload)
    assert result["segments"] == []
    assert "none survived normalization" in caplog.text


def test_normalize_output_silent_when_genuinely_empty(caplog) -> None:
    # An empty segments list is not evidence of a broken response — no warning.
    with caplog.at_level("WARNING"):
        _pyannote().normalize_output({"segments": []})
    assert caplog.text == ""


# --- async job protocol ---------------------------------------------------

import json
import httpx
import uuid as _uuid
from pathlib import Path


class _StubBackend(PyannoteBackend):
    """PyannoteBackend whose HTTP goes to a scripted MockTransport."""

    def __init__(self, handler):
        super().__init__("http://sidecar")
        self._handler = handler

    def _client(self, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler), timeout=timeout)


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


async def test_run_job_happy_path(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    seen = []

    def handler(request):
        if request.url.path == "/diarize":
            return httpx.Response(200, json={"job_id": "T", "state": "running"})
        if request.url.path == "/jobs/T/events":
            return httpx.Response(200, text=_sse(
                {"state": "running", "step": "embeddings", "completed": 1, "total": 4},
                {"state": "running", "step": "embeddings", "completed": 4, "total": 4},
                {"state": "done"},
            ), headers={"content-type": "text/event-stream"})
        if request.url.path == "/jobs/T/result":
            return httpx.Response(200, json={
                "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                "embeddings": {}, "num_speakers": 1,
            })
        return httpx.Response(404)

    async def on_progress(step, completed, total):
        seen.append((step, completed, total))

    backend = _StubBackend(handler)
    result = await backend.diarize(audio, job_id="T", on_progress=on_progress)

    assert result["num_speakers"] == 1
    assert (1, 4) == (seen[0][1], seen[0][2])  # progress was delivered
    assert seen[-1] == ("embeddings", 4, 4)


async def test_run_job_reattaches_when_already_done(tmp_path):
    """A worker that restarts after the job finished skips the stream."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    streamed = []

    def handler(request):
        if request.url.path == "/diarize":
            return httpx.Response(200, json={"job_id": "T", "state": "done"})
        if request.url.path == "/jobs/T/events":
            streamed.append(request.url.path)  # must NOT be hit
            return httpx.Response(200, text=_sse({"state": "done"}))
        if request.url.path == "/jobs/T/result":
            return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "speaker": "A"}],
                                             "embeddings": {}, "num_speakers": 1})
        return httpx.Response(404)

    result = await _StubBackend(handler).diarize(audio, job_id="T")
    assert result["num_speakers"] == 1
    assert streamed == []  # went straight to /result, no pointless stream


async def test_run_job_surfaces_failure(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    def handler(request):
        if request.url.path == "/diarize":
            return httpx.Response(200, json={"job_id": "T", "state": "running"})
        if request.url.path == "/jobs/T/events":
            return httpx.Response(200, text=_sse({"state": "failed", "error": "OOM"}),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    with pytest.raises(RuntimeError, match="OOM"):
        await _StubBackend(handler).diarize(audio, job_id="T")


async def test_run_job_reconnects_after_dropped_stream(tmp_path):
    """A dropped progress stream must be re-attached, not treated as failure."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    attempts = {"events": 0}

    def handler(request):
        if request.url.path == "/diarize":
            return httpx.Response(200, json={"job_id": "T", "state": "running"})
        if request.url.path == "/jobs/T/events":
            attempts["events"] += 1
            if attempts["events"] == 1:
                raise httpx.ReadError("connection dropped")  # mid-watch failure
            return httpx.Response(200, text=_sse({"state": "done"}),
                                  headers={"content-type": "text/event-stream"})
        if request.url.path == "/jobs/T/result":
            return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "speaker": "A"}],
                                             "embeddings": {}, "num_speakers": 1})
        return httpx.Response(404)

    result = await _StubBackend(handler).diarize(audio, job_id="T")
    assert result["num_speakers"] == 1
    assert attempts["events"] == 2  # reconnected once


async def test_run_job_records_retry_after_error(tmp_path, caplog):
    """The sidecar's one-shot error report must be logged, not dropped."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    def handler(request):
        if request.url.path == "/diarize":
            return httpx.Response(200, json={"job_id": "T", "state": "running",
                                             "retried_after_error": "prior OOM"})
        if request.url.path == "/jobs/T/events":
            return httpx.Response(200, text=_sse({"state": "done"}),
                                  headers={"content-type": "text/event-stream"})
        if request.url.path == "/jobs/T/result":
            return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "speaker": "A"}],
                                             "embeddings": {}, "num_speakers": 1})
        return httpx.Response(404)

    import logging
    with caplog.at_level(logging.WARNING):
        await _StubBackend(handler).diarize(audio, job_id="T")
    assert any("prior OOM" in r.message for r in caplog.records)


async def test_cancel_is_best_effort(tmp_path):
    """A cancel against an unreachable sidecar must not raise."""
    def handler(request):
        raise httpx.ConnectError("sidecar down")

    await _StubBackend(handler).cancel("T")  # must not raise


def test_timeout_for_upload_scales_with_size(tmp_path):
    from vts.services.diarization._base import timeout_for_upload, _UPLOAD_MIN_SECONDS

    small = tmp_path / "s.wav"
    small.write_bytes(b"x" * 1024)
    assert timeout_for_upload(small) == _UPLOAD_MIN_SECONDS  # floor holds

    big = tmp_path / "b.wav"
    big.write_bytes(b"x" * (100 * 1024 * 1024))
    assert timeout_for_upload(big) > _UPLOAD_MIN_SECONDS  # 100 MB exceeds the floor
