import pytest
from pydantic import ValidationError
from vts.api.schemas import RestartSummaryRequest, PromptRef
import uuid

TID = [uuid.uuid4()]

def test_prompts_allowed_with_final_only():
    req = RestartSummaryRequest(task_ids=TID, mode="final_only",
                                prompts=[PromptRef(source="system", id="summary")])
    assert req.prompts == [PromptRef(source="system", id="summary")]

def test_prompts_rejected_with_full():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="full",
                              prompts=[PromptRef(source="system", id="summary")])

def test_empty_prompts_rejected():
    with pytest.raises(ValidationError):
        RestartSummaryRequest(task_ids=TID, mode="final_only", prompts=[])

def test_none_prompts_ok_any_mode():
    assert RestartSummaryRequest(task_ids=TID, mode="full").prompts is None
    assert RestartSummaryRequest(task_ids=TID, mode="final_only").prompts is None
