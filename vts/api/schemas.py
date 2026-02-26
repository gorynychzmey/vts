from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    url: str = Field(min_length=3)
    language: str | None = None
    include_word_timestamps: bool = True
    force_reprocess: bool = False


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
