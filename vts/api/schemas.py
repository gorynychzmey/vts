from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, model_validator


class TaskCreateRequest(BaseModel):
    url: str = Field(min_length=3)
    language: str | None = None
    audio_only: bool = False
    transcript: bool = Field(default=True, validation_alias=AliasChoices("transcript", "do_transcribe"))
    summary: bool = Field(default=True, validation_alias=AliasChoices("summary", "do_summary"))

    @model_validator(mode="after")
    def validate_stage_dependencies(self) -> "TaskCreateRequest":
        if self.summary and not self.transcript:
            raise ValueError("summary requires transcript")
        return self


class StepOut(BaseModel):
    name: str
    status: str
    attempt: int
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


class TaskOut(BaseModel):
    id: UUID
    source_url: str
    status: str
    options: dict[str, Any]
    transcript_path: str | None
    summary_path: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    steps: list[StepOut]


class MessageOut(BaseModel):
    status: str


class MeOut(BaseModel):
    requested_by: str
    acting_as: str
    is_admin: bool


class AdminUsersOut(BaseModel):
    users: list[str]
