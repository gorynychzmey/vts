from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import HALFVEC, Vector

from vts.db.base import Base


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting = "waiting"
    paused = "paused"
    completed = "completed"
    archived = "archived"
    failed = "failed"
    canceled = "canceled"
    awaiting_input = "awaiting_input"


class StepStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class DeliveryStatus(StrEnum):
    pending = "pending"
    delivering = "delivering"
    delivered = "delivered"
    # No `failed`: a failing delivery goes back to `pending` (retry pending) or
    # to `dead` (attempts exhausted) — there was never a transition into it, so
    # it was dropped while the column was still free of production values.
    dead = "dead"
    # Plugin adapters are installed from external sources, so "target configured
    # but its adapter did not load this restart" is a normal transient state, not
    # a failure: the row parks here, spends no attempts, never reaches dead, and
    # delivers itself once the plugin returns. No migration needed for this value
    # — delivery_attempts.status is String(32), not a native enum.
    waiting_adapter = "waiting_adapter"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    preferred_ytdlp_client: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_preset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    tasks: Mapped[list["Task"]] = relationship(back_populates="user")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=False),
        nullable=False,
        default=TaskStatus.queued,
    )
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_dir: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_progress: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    awaiting_step: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="tasks")
    steps: Mapped[list["Step"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    asr_segments: Mapped[list["AsrSegment"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    __table_args__ = (
        Index("ix_tasks_user_created", "user_id", "created_at"),
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_source_url_status", "source_url", "status"),
    )


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus, name="step_status", native_enum=False), nullable=False, default=StepStatus.pending
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("task_id", "name", name="uq_steps_task_name"),
        Index("ix_steps_task_status", "task_id", "status"),
    )


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
        Index("ix_push_subscriptions_user", "user_id"),
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        Index("ix_api_tokens_user", "user_id", "revoked_at"),
    )


class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # True for a user's copy of a vendor prompt, False for one they wrote.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Answers one question: when did the *user* change this? NULL means never,
    # which is how the startup refresh tells an untouched vendor copy from an
    # edited one. Deliberately carries no default and no onupdate — a default
    # would override the NULL a fresh system copy needs, and the workaround
    # (a Core insert) breaks silently the moment someone writes session.add()
    # instead. Every write site sets it explicitly.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_prompts_user_created", "user_id", "created_at"),
        # One vendor copy per user. Without this, the API and the worker
        # creating the copy at the same moment both succeed and the user sees
        # a duplicate.
        Index(
            "ix_prompts_one_system_per_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("is_system"),
        ),
    )


class Preset(Base):
    __tablename__ = "presets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_presets_user_created", "user_id", "created_at"),
    )


class DeliveryCredential(Base):
    """One CONNECTION to an external system: endpoint plus its secrets.

    Split out of DeliveryTarget (vts-929) so that several destinations on the
    same server share one endpoint and one token. Two Outline instances with
    two collections each used to mean four targets duplicating base_url and
    api_token four times, making token rotation a four-row edit that is easy
    to get half-done.

    Which fields live here versus on the target is declared by the adapter's
    connection_fields(), never inferred by the core.
    """

    __tablename__ = "delivery_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secrets_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_delivery_credentials_user_name"),
        Index("ix_delivery_credentials_user", "user_id"),
    )


class DeliveryTarget(Base):
    __tablename__ = "delivery_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    # RESTRICT, not SET NULL: the reference is mandatory, so a credential that
    # still has targets must not be deletable out from under them. The repo
    # turns this into a clear 409 instead of an IntegrityError.
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("delivery_credentials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_delivery_targets_user_name"),
        Index("ix_delivery_targets_user", "user_id"),
        Index("ix_delivery_targets_credential", "credential_id"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delivery_targets.id", ondelete="SET NULL"), nullable=True
    )
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    # 64, not 32: besides "raw"/"redacted"/"summary" this now holds a prompt
    # ref like "user:<uuid>" (vts-as1i), which is 41 characters — the old
    # width would have failed the insert.
    variant: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", native_enum=False),
        nullable=False, default=DeliveryStatus.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_delivery_attempts_status_next", "status", "next_attempt_at"),
        Index("ix_delivery_attempts_task", "task_id"),
    )


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_speakers_user", "user_id"),
    )


class VoiceSample(Base):
    __tablename__ = "voice_samples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    speaker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speakers.id", ondelete="CASCADE"), nullable=False
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(256), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    audio: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    audio_format: Mapped[str] = mapped_column(String(32), nullable=False, default="wav")
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_voice_samples_speaker", "speaker_id"),
    )


class MatchDecision(Base):
    __tablename__ = "match_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    speaker_label: Mapped[str] = mapped_column(String, nullable=False)
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True
    )
    voice_sample_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_samples.id", ondelete="SET NULL"), nullable=True
    )
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    is_noise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_match_decisions_user", "user_id"),
    )


class Recording(Base):
    """A processed recording, outliving the task that produced it (vts-8w1r).

    Until now the task WAS the recording: deleting a task deleted the
    transcript, the media and the segments with it, because nothing else
    claimed them. A knowledge library needs the opposite — the recording is the
    lasting object, and a task is one way of creating or updating it.

    `source_task_id` is SET NULL rather than CASCADE, the same shape
    `voice_samples` already uses to outlive its task. The recording keeps its
    own `artifact_dir`, inherited from the task that produced it, so the files
    are not moved anywhere: ownership of the directory passes to the recording,
    and task deletion stops removing a directory a live recording still owns.

    `duration` and `language` are columns rather than derived on read. Duration
    used to be probed from the media file (cached in a sidecar NEXT TO that
    file), and language lived inside `Task.options` — so archiving a task, which
    deletes the media and the sidecar, lost both. They are the two facts a
    library list is built from, so they have to survive the media.
    """

    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The task that produced this recording, when it still exists. Nullable and
    # SET NULL: the recording is what lasts.
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_dir: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Seconds. Nullable because a recording whose media never arrived has no
    # duration to state — better an absent value than a fabricated zero.
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_recordings_user_created", "user_id", "created_at"),
        # One recording per task: a task creates or updates its recording, it
        # does not accumulate them. Partial, since source_task_id goes NULL when
        # the task is deleted and several such orphans may coexist.
        Index(
            "uq_recordings_source_task",
            "source_task_id",
            unique=True,
            postgresql_where=text("source_task_id IS NOT NULL"),
        ),
    )


class AsrSegment(Base):
    __tablename__ = "asr_segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Whisper's whole answer. Superseded by `payload` (vts-6qwy) and cleared
    # once a row has been decomposed; kept nullable rather than dropped so the
    # migration can be verified against the original before anything is lost.
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # The decomposed axes: {tokens, sentences, meta} — see services/asr_payload.
    # One JSON column rather than three: measured at 37 characters of difference
    # out of 60k, so the shape is a readability choice, not a size one.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    task: Mapped[Task] = relationship(back_populates="asr_segments")

    __table_args__ = (
        UniqueConstraint("task_id", "segment_index", name="uq_asr_segments_task_segment"),
        Index("ix_asr_segments_task_start", "task_id", "start_sec"),
    )


class TranscriptChunk(Base):
    """One retrievable passage of a recording, with its embedding (vts-twe7).

    Chunks hang off the RECORDING, not the task: the recording is what lasts,
    and a corpus that died with its tasks would be no corpus at all. Re-indexing
    replaces a recording's chunks wholesale, which is what makes it safe to run
    again after `rerender_transcript` changes the text.

    `speakers` holds the technical SPEAKER_NN tags, never display names. Names
    are substituted at render time, so a speaker rename does not invalidate the
    index — the same property the player already relies on.

    The embedding is HALFVEC, not VECTOR. bge-m3 is 1024-dimensional, so float4
    costs 4 KB per chunk against 2 KB in fp16, and vectors — not text — become
    the bulk of this database as soon as a corpus exists. Measured before
    choosing: fp16 shifts cosine scores by 0.00001, which is 0.01% of the
    0.379..0.521 band that separates answerable queries from unanswerable ones,
    and changes no ranking. TOAST does not help here: these are dense binary
    values, so the type is the only lever.
    """

    __tablename__ = "transcript_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    speakers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("recording_id", "chunk_index", name="uq_chunks_recording_index"),
        Index("ix_chunks_recording", "recording_id"),
        # Scoping every search to one user is not an optimisation, it is the
        # access rule — so the column it filters on carries an index.
        Index("ix_chunks_user", "user_id"),
    )


class UserStepWeights(Base):
    __tablename__ = "user_step_weights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    final_summary_fallback: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sample_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_step_weights_user"),
        Index("ix_user_step_weights_user", "user_id"),
    )
