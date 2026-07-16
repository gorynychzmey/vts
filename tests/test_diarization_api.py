import pytest
from pydantic import ValidationError

from vts.api.schemas import PresetOptions, TaskCreateRequest, UploadInitRequest


def test_diarize_defaults_to_false() -> None:
    assert PresetOptions().diarize is False
    assert TaskCreateRequest(url="https://example.com/v").diarize is False
    assert UploadInitRequest(filename="a.mp4", total_size=1).diarize is False


def test_diarize_accepted() -> None:
    assert TaskCreateRequest(url="https://example.com/v", diarize=True).diarize is True


def test_diarize_requires_transcript() -> None:
    # There is nothing to attribute speakers to without a transcript.
    with pytest.raises(ValidationError, match="diarize requires transcript"):
        TaskCreateRequest(
            url="https://example.com/v",
            diarize=True,
            transcript=False,
            prompts=[],
        )
