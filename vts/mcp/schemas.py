from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


TaskStatusLiteral = Literal[
    "queued", "running", "paused", "completed", "archived", "failed", "canceled"
]


class ProgressCounts(BaseModel):
    """Discrete progress counts for a pipeline stage."""
    current: int
    total: int


class SubmitVideoResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    created_at: datetime


class TaskSummary(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    title: str | None
    url: str
    created_at: datetime
    updated_at: datetime


class TaskStatusResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    stage: str | None
    progress: ProgressCounts | None
    error: str | None
    updated_at: datetime


class TranscriptResult(BaseModel):
    task_id: uuid.UUID
    variant: Literal["raw", "redacted"]
    content: str
    format: Literal["txt", "json"]


class PromptInfo(BaseModel):
    """One prompt available to the calling user (system or user-defined)."""
    source: Literal["system", "user"]
    id: str
    name: str
    editable: bool


class PresetInfo(BaseModel):
    """One preset available to the calling user (system or user-defined)."""
    source: Literal["system", "user"]
    id: str
    name: str
    editable: bool
    options: dict


class PromptResult(BaseModel):
    task_id: uuid.UUID
    source: str
    id: str
    content: str


class WaitResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    reached: bool
    stage: str | None
    updated_at: datetime
