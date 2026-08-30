from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class PromptRef(BaseModel):
    source: Literal["system", "user"]
    id: str = Field(min_length=1)


class PromptOut(BaseModel):
    source: str
    id: str
    name: str
    editable: bool
    # True for the user's copy of a vendor prompt: the editor offers "Restore"
    # instead of "Delete" for it.
    is_system: bool = False


class PromptDetailOut(BaseModel):
    source: str
    id: str
    name: str
    system_prompt: str
    editable: bool


class SystemPromptTextOut(BaseModel):
    system_prompt: str


class PromptCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    system_prompt: str = Field(min_length=1)


class PromptUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    system_prompt: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> "PromptUpdateRequest":
        # name is optional (None = leave unchanged), but when provided it must
        # be non-empty after trimming — consistent with create's min_length=1.
        if self.name is not None:
            stripped = self.name.strip()
            if not stripped:
                raise ValueError("name must not be blank")
            self.name = stripped
        return self


class PresetRef(BaseModel):
    source: Literal["system", "user"]
    id: str = Field(min_length=1)


class DeliveryRef(BaseModel):
    """A reference to a delivery target by ID, never secrets."""

    deliver_to: str = Field(min_length=1)
    # Not a Literal: besides the three fixed artifacts, `variant` may address
    # a prompt result as "source:id" (vts-as1i). Validated below rather than
    # by the type, so the error names what was wrong.
    variant: str | None = None

    @field_validator("variant")
    @classmethod
    def _validate_variant(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value in ("raw", "redacted", "summary"):
            return value
        from vts.services.prompt_registry import parse_ref

        try:
            parse_ref(value)
        except ValueError as exc:
            raise ValueError(
                f"variant must be raw/redacted/summary or a prompt ref "
                f"like 'user:<uuid>', got {value!r}"
            ) from exc
        return value


class PresetOptions(BaseModel):
    language: str | None = None
    audio_only: bool = False
    transcript: bool = True
    diarize: bool = False
    prompts: list[PromptRef] = Field(default_factory=list)
    delivery: list[DeliveryRef] = Field(default_factory=list)


class PresetOut(BaseModel):
    source: str
    id: str
    name: str
    options: PresetOptions
    editable: bool


class ProgressWeightsOut(BaseModel):
    weights: dict[str, float]
    final_summary_fallback: float


class DeliveryOut(BaseModel):
    id: str
    adapter: str
    variant: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    external_url: str | None
    # True while the row is parked waiting for its plugin to come back. Lets the
    # UI say "waiting for plugin" instead of rendering it as a failure.
    waiting_for_adapter: bool


class DeliveryRetryRequest(BaseModel):
    target_id: UUID | None = None


class DeliveryAdapterOut(BaseModel):
    """What the UI needs to render forms for one installed adapter.

    The core does not know which fields mean what; the adapter declares its
    schema and which of its fields form the connection, and the UI splits the
    credential form from the target form on that basis (vts-929).
    """

    name: str
    config_schema: dict
    secret_keys: list[str]
    connection_fields: list[str]
    #: Fields whose values the adapter can enumerate from the external system
    #: (contract 1.2). The UI renders these as a picker instead of free text.
    #: Kept out of `config_schema` so that stays standard JSON Schema.
    option_fields: list[str] = Field(default_factory=list)
    #: Whether the adapter implements check_connection, so the UI knows
    #: whether to offer the button at all.
    supports_check: bool = False


class DeliveryCheckOut(BaseModel):
    """Result of testing a connection (vts-6o37)."""

    ok: bool
    #: A CheckOutcome value. The UI maps it to a localised message, so the
    #: server never sends user-facing prose here.
    outcome: str
    #: Optional untranslated context (a status line, an exception message).
    detail: str | None = None


class DeliveryOptionOut(BaseModel):
    """One enumerated choice for a config field: stored value + shown label."""

    value: str
    label: str


class DeliveryOptionsOut(BaseModel):
    options: list[DeliveryOptionOut] = Field(default_factory=list)


class DeliveryVariantOut(BaseModel):
    """One choice for WHICH artifact of a task a target delivers."""

    #: What goes into a target's `default_variant`: a fixed name ("raw",
    #: "redacted", "summary") or a prompt ref ("user:<uuid>").
    value: str
    label: str


class DeliveryAdaptersOut(BaseModel):
    adapters: list[DeliveryAdapterOut]
    # Offered by the CORE, not by any adapter (vts-6fya): which artifact to
    # deliver is the core's decision, and the valid values include the
    # caller's own prompts — something a plugin's static schema could never
    # enumerate. Per-user, so this response must not be cached across users.
    variants: list[DeliveryVariantOut] = Field(default_factory=list)
    # Adapters that were found but refused at load time, name -> reason
    # (vts-9y7). Surfaced so an operator can see WHY a target's plugin is
    # unavailable instead of watching it silently vanish.
    incompatible: dict[str, str] = Field(default_factory=dict)


class DeliveryCredentialCreate(BaseModel):
    """One connection to an external system: endpoint plus secrets (vts-929)."""

    name: str = Field(min_length=1, max_length=255)
    adapter: str = Field(min_length=1, max_length=64)
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str] | None = None


class DeliveryCredentialUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    config: dict | None = None
    secrets: dict[str, str] | None = None
    clear_secrets: bool = False

    @model_validator(mode="after")
    def _validate_name(self) -> "DeliveryCredentialUpdate":
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip() if self.name is not None else None
        return self


class DeliveryCredentialOut(BaseModel):
    id: str
    name: str
    adapter: str
    config: dict
    # Presence markers only — {"api_token": {"set": true}}. Secret VALUES are
    # write-only and must never appear in a response.
    secrets: dict[str, dict]
    # False when this credential's adapter plugin is not loaded right now.
    adapter_available: bool
    # How many targets reference this credential. Deleting one that is in use
    # is refused, so the count is what makes that refusal actionable.
    used_by: int = 0


class DeliveryTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    adapter: str = Field(min_length=1, max_length=64)
    # Mandatory: a target is only ever the per-destination half of a config;
    # the connection always comes from a credential (vts-929).
    credential_id: str = Field(min_length=1)
    config: dict = Field(default_factory=dict)


class DeliveryTargetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    config: dict | None = None
    credential_id: str | None = None

    @model_validator(mode="after")
    def _validate_name(self) -> "DeliveryTargetUpdate":
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip() if self.name is not None else None
        return self


class DeliveryTargetOut(BaseModel):
    id: str
    name: str
    adapter: str
    credential_id: str
    config: dict
    # False when this target's adapter plugin is not loaded right now. The target
    # is still listed (settings must survive a plugin being temporarily absent);
    # it simply cannot be picked for a new task until the adapter is back.
    adapter_available: bool


class PresetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    options: PresetOptions


class PresetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    options: PresetOptions | None = None

    @model_validator(mode="after")
    def _validate_name(self) -> "PresetUpdateRequest":
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        self.name = self.name.strip() if self.name is not None else None
        return self


def _default_prompts() -> list["PromptRef"]:
    return [PromptRef(source="system", id="summary")]


class TaskCreateRequest(BaseModel):
    url: str = Field(min_length=3)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Reject non-http(s) and internal targets before anything is queued.

        The worker fetches this with yt-dlp from inside the podman network, so
        an unchecked URL is a server-side request primitive (vts-h45).
        """
        from vts.services.source_url import InvalidSourceUrl, validate_source_url

        try:
            return validate_source_url(value)
        except InvalidSourceUrl as exc:
            raise ValueError(str(exc)) from exc
    language: str | None = None
    audio_only: bool = False
    transcript: bool = Field(default=True, validation_alias=AliasChoices("transcript", "do_transcribe"))
    diarize: bool = False
    # Skips the awaiting_input pause in MatchSpeakersStep: read via
    # ctx.task_flag(task_options, "speaker_no_manual_stop", ...) (vts-80i).
    # Meaningless without diarize, same dependency shape as diarize/transcript.
    speaker_no_manual_stop: bool = False
    prompts: list[PromptRef] = Field(default_factory=_default_prompts)
    # Optional delivery destinations, by target name. Validated against the
    # caller's targets (and their adapters' availability) in the endpoint.
    delivery: list[DeliveryRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stage_dependencies(self) -> "TaskCreateRequest":
        if self.prompts and not self.transcript:
            raise ValueError("prompts require transcript")
        if any(d.variant == "summary" for d in self.delivery) and not self.prompts:
            raise ValueError("delivery variant 'summary' requires prompts")
        # Delivering a prompt's result requires that prompt to actually run,
        # otherwise the delivery is queued for an artifact nothing will ever
        # produce and only fails much later, at delivery time (vts-as1i).
        selected = {f"{p.source}:{p.id}" for p in self.prompts}
        for entry in self.delivery:
            variant = entry.variant or ""
            if ":" not in variant:
                continue
            if variant not in selected:
                raise ValueError(
                    f"delivery variant {variant!r} needs that prompt selected"
                )
        if self.diarize and not self.transcript:
            raise ValueError("diarize requires transcript")
        if self.speaker_no_manual_stop and not self.diarize:
            raise ValueError("speaker_no_manual_stop requires diarize")
        return self


class TaskUpdate(BaseModel):
    display_name: str | None = None


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
    media_seconds: int | None = Field(default=None, ge=0)
    media_bytes: int | None = Field(default=None, ge=0)
    # None means diarization did not run (or its output is unreadable); 0 means
    # it ran and found nobody. The card distinguishes the two — it shows nothing
    # for None rather than a misleading "0 speakers".
    speaker_count: int | None = Field(default=None, ge=0)


class TaskCapabilities(BaseModel):
    """Task-DEPENDENT capabilities (read task.steps/options), so they ship
    per-task. Pure-status flags (pause/resume/archive) are NOT here — they
    are delivered once via GET /api/status-config."""
    can_restart_summary: bool = False
    can_restart_final_summary: bool = False
    can_resolve_speakers: bool = False


class TaskOut(BaseModel):
    id: UUID
    source_url: str
    source_title: str | None = None
    status: str
    # Which manual-input step a `awaiting_input` task is paused on (currently
    # only "match_speakers"); null otherwise. Drives which dialog "Доработать"
    # opens (vts-80i).
    awaiting_step: str | None = None
    queue_position: int | None = Field(default=None, ge=1)
    queue: str | None = None
    capabilities: TaskCapabilities = Field(default_factory=TaskCapabilities)
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

    @field_serializer("created_at")
    def _ser_created_at(self, v: datetime) -> str:
        # Fixed-width microseconds so lexicographic string comparison of two
        # same-second timestamps is correct (client isNewerThan / cursor strings).
        # Pydantic's default omits fractional seconds when us==0, which breaks
        # "...56Z" vs "...56.000001Z" ordering (Z > '.'). Force 6 digits.
        return v.isoformat(timespec="microseconds")


class TaskCompactOut(BaseModel):
    """Slimmed-down task representation for list views.

    Drops `steps`, `options`, `error_message`, `*_path`. Roughly an order
    of magnitude smaller per task than `TaskOut`, which matters when the
    client has a small response budget (ChatGPT Custom Actions cap at ~30KB).
    """
    id: UUID
    source_url: str
    source_title: str | None = None
    status: str
    awaiting_step: str | None = None
    queue_position: int | None = Field(default=None, ge=1)
    queue: str | None = None
    capabilities: TaskCapabilities = Field(default_factory=TaskCapabilities)
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime
    progress: TaskProgressOut = Field(default_factory=TaskProgressOut)
    stats: TaskStatsOut = Field(default_factory=TaskStatsOut)

    @field_serializer("created_at")
    def _ser_created_at(self, v: datetime) -> str:
        # See TaskOut._ser_created_at: fixed-width microseconds keep
        # lexicographic string ordering of created_at consistent with
        # chronological ordering (VOS-84 pagination/banner cursors).
        return v.isoformat(timespec="microseconds")


class TaskIdsRequest(BaseModel):
    task_ids: list[UUID] = Field(min_length=1, max_length=100)


class RestartSummaryRequest(BaseModel):
    mode: Literal["full", "final_only"] = "full"
    prompts: list[PromptRef] | None = None

    @model_validator(mode="after")
    def _validate_prompts(self) -> "RestartSummaryRequest":
        if self.prompts is not None:
            if self.mode != "final_only":
                raise ValueError("prompts is only allowed with mode=final_only")
            if len(self.prompts) == 0:
                raise ValueError("prompts must not be empty")
        return self


class RecordingOut(BaseModel):
    """A recording in the library (vts-8w1r).

    Deliberately not a TaskOut: a recording outlives the task that produced it,
    so it carries no status, no progress and no steps. `source_task_id` is null
    once that task is gone — the recording is what lasts.

    `duration_sec` and `language` are stored rather than derived, so they are
    still here after the media has been archived away.
    """

    id: UUID
    source_task_id: UUID | None = None
    title: str | None = None
    source_url: str | None = None
    duration_sec: float | None = None
    language: str | None = None
    tags: list[str] = []
    has_transcript: bool = False
    has_summary: bool = False
    has_media: bool = False
    recorded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SearchHitOut(BaseModel):
    """One passage returned by corpus search (vts-uurt).

    `recording_id` is the stable identifier a client holds on to and can use to
    fetch the whole transcript later — deliberately not the task id, which goes
    away when the task is deleted while the recording remains.
    """

    chunk_id: UUID
    recording_id: UUID
    # For building /player/{task}?t=; null once the task is deleted, which
    # means the passage is still real but no longer openable in a player.
    source_task_id: UUID | None = None
    title: str | None = None
    text: str
    start_sec: float
    end_sec: float
    speakers: list[str] = []
    score: float


class SearchResultOut(BaseModel):
    """Search results, with the threshold that produced them.

    `threshold` is echoed back because an empty `hits` means "nothing in the
    corpus is this relevant" rather than "the corpus is empty" — and the caller
    cannot tell those apart without knowing where the bar was set.
    """

    hits: list[SearchHitOut]
    threshold: float
    query: str


class RecordingListOut(BaseModel):
    items: list[RecordingOut]
    total: int


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


class TextSliceOut(BaseModel):
    """Paginated slice of a text artifact (transcript, summary, log).

    Returned in JSON mode (Accept: application/json) for endpoints whose
    plain-text form can exceed external clients' response budget
    (notably ChatGPT Custom Actions: ~30KB cap). Default plain-text mode
    is unaffected.

    `text` is the slice itself; the other fields let the caller iterate
    without an extra HEAD-style request.
    """
    text: str
    offset: int = Field(ge=0)
    length: int = Field(ge=0)
    total_length: int = Field(ge=0)
    is_end: bool


# ---------------------------------------------------------------------------
# Chunked upload schemas (vts-b8j)
# ---------------------------------------------------------------------------

class UploadConfigOut(BaseModel):
    chunked_threshold_bytes: int
    chunk_bytes: int
    max_upload_bytes: int


class UploadFileSpec(BaseModel):
    filename: str
    total_size: int
    # File.lastModified from the browser, epoch ms. Used only as the second
    # ordering signal, after the container's own creation_time (vts-vm0).
    last_modified: int | None = None


class UploadInitRequest(BaseModel):
    # Required for the legacy single-file flow; a multi-file request (`files`
    # non-empty) leaves these at their defaults and is not lying about a
    # filename/size it doesn't have — the handler never reads them in that
    # branch. The model validator below still requires them when `files` is
    # absent, so the single-file contract hasn't changed (vts-vm0).
    filename: str = ""
    total_size: int = 0
    language: str | None = None
    audio_only: bool = False
    transcript: bool = True
    diarize: bool = False
    prompts: str | None = None
    # JSON list of {deliver_to: "<target uuid>", variant?}, matching `prompts`
    # above: this flow carries its options as a sidecar rather than a request
    # model, so the list arrives encoded (vts-j2kh).
    delivery: str | None = None
    display_name: str | None = None
    # When present, this is a multi-file set and `filename`/`total_size` above
    # are ignored. Absent means the existing single-file flow (vts-vm0).
    files: list[UploadFileSpec] | None = None

    @model_validator(mode="after")
    def _require_legacy_fields_or_files(self) -> "UploadInitRequest":
        if self.files:
            return self
        if not self.filename or self.total_size <= 0:
            raise ValueError("filename and total_size are required when files is absent")
        return self


class UploadInitOut(BaseModel):
    upload_id: str
    chunk_size: int
    files: list[dict] | None = None


class UploadOffsetOut(BaseModel):
    received: int
    total_size: int


# ---------------------------------------------------------------------------
# Speaker registry (vts-80i)
# ---------------------------------------------------------------------------


class SpeakerOut(BaseModel):
    id: str
    name: str
    sample_count: int


class SpeakerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SpeakerUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MoveVoiceSampleRequest(BaseModel):
    target_speaker_id: UUID


class MoveCandidateOut(BaseModel):
    id: str
    name: str
    sample_count: int
    # None when the candidate has no fragment from the same embedding model,
    # so the UI can show "—" instead of a fabricated distance.
    distance: float | None = None


class MergeSpeakersRequest(BaseModel):
    target_id: UUID


class VoiceSampleOut(BaseModel):
    id: str
    duration_sec: float
    source_task_id: str | None = None
    created_at: datetime


class VoiceResolution(BaseModel):
    speaker_label: str
    action: str  # "bind_existing" | "bind_new" | "leave_anonymous" | "accept_auto"
    speaker_id: str | None = None
    new_name: str | None = None
    add_fragment: bool = True
    distance: float | None = None
    voice_sample_id: str | None = None  # winning fragment for the decision
    outcome: str  # confirmed | rejected | manual_match | auto_accepted | auto_overridden | left_anonymous
    is_noise: bool = False


class VoiceResolutionRequest(BaseModel):
    resolutions: list[VoiceResolution]
    continue_task: bool
