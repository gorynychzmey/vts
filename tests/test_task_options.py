import pytest
from pydantic import ValidationError

from vts.api.schemas import TaskCreateRequest


def test_task_options_defaults() -> None:
    payload = TaskCreateRequest(url="https://example.com/video")
    assert payload.audio_only is False
    assert payload.transcript is True
    assert payload.summary is True


def test_summary_requires_transcript() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest(
            url="https://example.com/video",
            transcript=False,
            summary=True,
        )


def test_aliases_do_transcribe_do_summary() -> None:
    payload = TaskCreateRequest.model_validate(
        {
            "url": "https://example.com/video",
            "audio_only": True,
            "do_transcribe": True,
            "do_summary": False,
        }
    )
    assert payload.audio_only is True
    assert payload.transcript is True
    assert payload.summary is False
