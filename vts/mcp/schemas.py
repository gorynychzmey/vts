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


class RecordingInfo(BaseModel):
    """A recording in the library — the lasting object, not the job.

    `source_task_id` is null once the task that produced it has been deleted;
    the recording and its artifacts are unaffected, which is the whole reason
    these tools address recordings rather than tasks.
    """

    id: uuid.UUID
    source_task_id: uuid.UUID | None = None
    title: str | None = None
    duration_sec: float | None = None
    language: str | None = None
    has_transcript: bool = False
    has_summary: bool = False
    has_media: bool = False
    created_at: datetime


class RecordingList(BaseModel):
    items: list[RecordingInfo]
    total: int


class TranscriptEntry(BaseModel):
    """One timed line of a transcript.

    Timecodes and the speaker are what make a quote checkable — flat text can
    be read but not cited.
    """

    start_sec: float
    end_sec: float
    text: str
    speaker: str | None = None


class RecordingTranscript(BaseModel):
    """A recording's transcript, optionally windowed around a moment.

    `entries` is empty for a plain-text read; ask for the structured form to
    get them. When `around_sec` was given, only the passage around that second
    is returned — a two-hour transcript fetched to show one quote buries the
    part that mattered.
    """

    recording_id: uuid.UUID
    title: str | None = None
    variant: str
    content: str = ""
    entries: list[TranscriptEntry] = []
    around_sec: float | None = None


class SearchHit(BaseModel):
    """One passage of a recording that matched a corpus search.

    `recording_id` is the stable identifier to hold on to and to fetch the full
    transcript with later — not the task id, which disappears when a task is
    deleted while the recording remains.
    """

    recording_id: uuid.UUID
    # Present so a client can open /player/{task}?t=<start_sec>; null once the
    # task is gone, in which case the passage is still citable but no longer
    # has a player page.
    source_task_id: uuid.UUID | None = None
    title: str | None = None
    text: str
    start_sec: float
    end_sec: float
    speakers: list[str] = []
    score: float
    # Read the passage — via the RECORDING, so it survives the task's deletion.
    transcript_url: str | None = None
    # Watch the moment — via the TASK, so it is null once that task is gone.
    player_url: str | None = None


class SearchResult(BaseModel):
    """Evidence for a question, never an answer to it.

    VOS-132 is explicit that VTS stays a retrieval server: the reasoning
    belongs to the client. So this returns passages with their positions and
    scores and nothing that resembles a composed reply.

    An empty `hits` means nothing in the corpus is relevant enough — NOT that
    the corpus is empty, and not that the nearest passages were withheld
    arbitrarily. `threshold` is returned so a client can tell those apart, and
    say so to its user rather than inventing an answer from weak matches.
    """

    query: str
    threshold: float
    hits: list[SearchHit]


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
