from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


TaskStatusLiteral = Literal[
    "queued", "running", "waiting", "paused", "completed", "archived", "failed", "canceled"
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


class TaskPage(BaseModel):
    """One page of tasks plus an opaque cursor for the next page."""
    tasks: list[TaskSummary]
    next_cursor: str | None = None
    has_more: bool = False


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


class DeliveryCredentialInfo(BaseModel):
    """One connection to an external system. Secret VALUES are never included."""
    id: str
    name: str
    adapter: str
    config: dict
    secrets: dict[str, dict]  # {key: {"set": bool}} — presence only
    adapter_available: bool
    used_by: int = 0  # how many targets reference this connection


class DeliveryTargetInfo(BaseModel):
    """One delivery target: a destination hanging off a connection.

    Secrets live on the connection (see DeliveryCredentialInfo), not here.
    """
    id: str
    name: str
    adapter: str
    credential_id: str
    config: dict
    adapter_available: bool


class DeliveryStatusInfo(BaseModel):
    """State of one delivery of one task to one target."""
    id: str
    adapter: str
    variant: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    external_url: str | None
    waiting_for_adapter: bool


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
