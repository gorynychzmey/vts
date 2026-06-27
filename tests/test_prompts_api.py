import pytest
from pydantic import ValidationError
from vts.api.schemas import (
    PromptRef, PromptCreateRequest, TaskCreateRequest,
)


def test_task_create_defaults_to_summary():
    req = TaskCreateRequest(url="https://x/y")
    assert req.prompts == [PromptRef(source="system", id="summary")]


def test_task_create_empty_prompts_allowed_without_summary():
    req = TaskCreateRequest(url="https://x/y", prompts=[])
    assert req.prompts == []


def test_non_empty_prompts_requires_transcript():
    with pytest.raises(ValidationError):
        TaskCreateRequest(url="https://x/y", transcript=False,
                          prompts=[PromptRef(source="system", id="summary")])


def test_prompt_create_request_validates():
    with pytest.raises(ValidationError):
        PromptCreateRequest(name="", system_prompt="x")
