from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
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


class StageProgressOut(BaseModel):
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


class TaskProgressOut(BaseModel):
    transcribe: StageProgressOut = Field(default_factory=StageProgressOut)
    summary: StageProgressOut = Field(default_factory=StageProgressOut)


class TaskStatsOut(BaseModel):
    processing_seconds: int | None = Field(default=None, ge=0)
    transcript_chars: int | None = Field(default=None, ge=0)
    summary_chars: int | None = Field(default=None, ge=0)
    redacted_chars: int | None = Field(default=None, ge=0)


class TaskOut(BaseModel):
    id: UUID
    source_url: str
    source_title: str | None = None
    status: str
    queue_position: int | None = Field(default=None, ge=1)
    options: dict[str, Any]
    transcript_path: str | None
    summary_path: str | None
    redacted_path: str | None = None
    media_path: str | None = None
    error_message: str | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    steps: list[StepOut]
    progress: TaskProgressOut = Field(default_factory=TaskProgressOut)
    stats: TaskStatsOut = Field(default_factory=TaskStatsOut)


class TaskIdsRequest(BaseModel):
    task_ids: list[UUID] = Field(min_length=1, max_length=100)


class RestartSummaryRequest(BaseModel):
    task_ids: list[UUID] = Field(min_length=1, max_length=100)
    mode: Literal["full", "final_only"] = "full"


class BatchResultOut(BaseModel):
    results: dict[str, str]


class MessageOut(BaseModel):
    status: str


class MeOut(BaseModel):
    requested_by: str
    acting_as: str
    is_admin: bool


class AdminUsersOut(BaseModel):
    users: list[str]


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10)
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)
    user_agent: str | None = None


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=10)


class PushConfigOut(BaseModel):
    enabled: bool
    public_key: str | None = None


class PushStatusOut(BaseModel):
    subscribed: bool
    endpoint: str | None = None


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ApiTokenOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None


class ApiTokenCreateOut(ApiTokenOut):
    # The raw token, returned only at creation time. Never persisted by the
    # server in clear; never re-fetchable through GET.
    token: str
