from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


TaskStatusLiteral = Literal[
    "queued", "running", "paused", "completed", "archived", "failed", "canceled"
]


class SubmitVideoResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    created_at: datetime


class TaskSummary(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    title: str | None
    url: str | None
    created_at: datetime
    updated_at: datetime


class TaskStatusResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    stage: str | None
    asr_progress: float
    summary_progress: float
    error: str | None
    updated_at: datetime


class TranscriptResult(BaseModel):
    task_id: uuid.UUID
    variant: Literal["raw", "redacted"]
    content: str
    format: Literal["txt", "json"]


class SummaryResult(BaseModel):
    task_id: uuid.UUID
    content: str
    format: Literal["markdown"]


class WaitResult(BaseModel):
    task_id: uuid.UUID
    status: TaskStatusLiteral
    reached: bool
    stage: str | None
    updated_at: datetime
