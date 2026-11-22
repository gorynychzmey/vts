from __future__ import annotations

from collections.abc import Iterable
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import true as sa_true
from sqlalchemy import Text, and_, delete, exists, false, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload, undefer

from vts.db.models import (
    ApiToken,
    AsrSegment,
    DeliveryAttempt,
    DeliveryCredential,
    DeliveryStatus,
    DeliveryTarget,
    MatchDecision,
    Recording,
    RecordingState,
    Preset,
    Prompt,
    Speaker,
    RecordingTag,
    Step,
    StepStatus,
    Tag,
    Task,
    TaskStatus,
    User,
    UserStepWeights,
    VoiceSample,
)
from vts.metrics.step_weights import StepDuration
from vts.services import task_status
from vts.services.asr_payload import decompose_raw_json
from vts.services.tags import TagExists, TagNameError, dedupe_names, normalize_tag_name, tag_key
from vts.services.recording_meta import language_code, recording_display_name
from vts.services.recording_state import initial_state, state_for_status
from vts.services.delivery_status import RETRYABLE_STATUSES

# Sentinel for "argument not given", distinct from an explicit None (which
# clears a field).
UNSET: Any = object()


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# How many completed tasks for the same URL find_completed_donor() examines,
# newest first. Bounded rather than unbounded: the point is to see past a newer
# task whose options differ (vts-wpy), and a URL processed more than this many
# times with the wanted options never matching is not worth an unbounded scan.
_DONOR_CANDIDATE_LIMIT = 20



def _tag_clause(
    tags: list[str],
    tag_match: Literal["all", "any"],
    recording_link,
):
    """WHERE clause for "the recording carries these tags" (vts-qwcj).

    One correlated EXISTS per tag key, so the database does the matching and no
    id list is loaded into Python. `recording_link(rec)` ties the aliased
    recording `rec` to the outer row (the recording itself, or the task it was
    made from). The tag must belong to the recording's own user, so another
    user's tag of the same key can never match.

    Names that cannot be tags (empty, too long) are unknown names: with "all"
    one unknown name empties the result, with "any" it is ignored and only an
    all-unknown list empties it.
    """
    keys: list[str] = []
    unknown = False
    for raw in tags:
        try:
            key = tag_key(raw)
        except TagNameError:
            unknown = True
            continue
        if key not in keys:
            keys.append(key)
    if tag_match == "all" and unknown:
        return false()
    if not keys:
        return false()

    def has(key: str):
        rec = aliased(Recording)
        return exists(
            select(1)
            .select_from(RecordingTag)
            .join(Tag, Tag.id == RecordingTag.tag_id)
            .join(rec, rec.id == RecordingTag.recording_id)
            .where(Tag.name_key == key, Tag.user_id == rec.user_id, recording_link(rec))
        )

    clauses = [has(k) for k in keys]
    return and_(*clauses) if tag_match == "all" else or_(*clauses)


class Repo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_user(self, username: str) -> User:
        stmt = select(User).where(User.username == username)
        user = await self.session.scalar(stmt)
        if user:
            return user
        # Check-then-insert races: two concurrent first requests for the same
        # new user both saw "no row" and the loser hit the unique constraint as
        # a 500 (vts-76y). Insert with ON CONFLICT DO NOTHING, then re-select —
        # whoever lost the race reads the winner's row.
        await self.session.execute(
            pg_insert(User.__table__)
            .values(username=username)
            .on_conflict_do_nothing(index_elements=[User.__table__.c.username])
        )
        await self.session.flush()
        user = await self.session.scalar(stmt)
        if user is None:  # pragma: no cover - the row was just inserted
            raise RuntimeError(f"user {username!r} vanished after insert")
        return user

    async def list_usernames(self) -> list[str]:
        stmt = select(User.username).order_by(User.username.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_user_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return await self.session.scalar(stmt)

    async def get_user_preferred_ytdlp_client(self, user_id: uuid.UUID) -> str | None:
        stmt = select(User.preferred_ytdlp_client).where(User.id == user_id)
        return await self.session.scalar(stmt)

    async def set_user_preferred_ytdlp_client(self, user_id: uuid.UUID, player_client: str | None) -> None:
        stmt = select(User).where(User.id == user_id)
        user = await self.session.scalar(stmt)
        if user is None:
            return
        user.preferred_ytdlp_client = player_client
        await self.session.flush()

    async def create_task(
        self,
        user_id: uuid.UUID,
        source_url: str,
        options: dict[str, object],
        artifact_dir: str,
        task_id: uuid.UUID | None = None,
        source_title: str | None = None,
    ) -> Task:
        task = Task(
            id=task_id or uuid.uuid4(),
            user_id=user_id,
            source_url=source_url,
            source_title=source_title,
            options=options,
            artifact_dir=artifact_dir,
            status=TaskStatus.queued,
        )
        self.session.add(task)
        # Flushed first: the flush assigns the defaults (created_at) that the
        # recording copies below.
        await self.session.flush()
        # The recording exists from the moment the task does, so its state can
        # follow the pipeline (vts-lzad). It stays out of the library until it
        # has text.
        self.session.add(
            Recording(
                user_id=user_id,
                source_task_id=task.id,
                artifact_dir=artifact_dir,
                title=recording_display_name(source_title, source_url),
                source_url=source_url,
                recorded_at=task.created_at,
                state=RecordingState.processing,
                # An explicit choice is known now (later updates come from
                # set_detected_language and upsert_recording_for_task). Without
                # it the player and subtitles of a running task, which read the
                # recording, labelled voices in English (vts-lzad).
                language=language_code(
                    options.get("language") if isinstance(options, dict) else None
                ),
            )
        )
        await self.session.flush()
        return task

    async def list_tasks_for_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id)
            .order_by(Task.created_at.desc())
        )
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    @staticmethod
    def _apply_task_filters(
        stmt,
        *,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        source_type: str | None = None,
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
    ):
        """Narrow a Task query by search text, creation date, source type and
        the tags of the task's recording (a task without one has no tags).

        Shared by the cursor page and the legacy list so the two cannot drift.
        These only NARROW the set — the cursor/order logic is untouched, the
        same contract `status` already follows.
        """
        if q:
            # Escape the LIKE wildcards, otherwise a literal "%" or "_" typed
            # by the user silently becomes a pattern and matches far too much.
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            needle = f"%{escaped}%"
            stmt = stmt.where(
                or_(
                    Task.source_title.ilike(needle, escape="\\"),
                    Task.source_url.ilike(needle, escape="\\"),
                )
            )
        if created_from is not None:
            stmt = stmt.where(Task.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(Task.created_at <= created_to)
        if source_type == "file":
            # Uploads are recorded as file://<name>; everything else is a URL
            # the pipeline downloads. There is no separate column for this —
            # the prefix is what the rest of the codebase keys on too.
            stmt = stmt.where(Task.source_url.startswith("file://"))
        elif source_type == "url":
            stmt = stmt.where(~Task.source_url.startswith("file://"))
        if tags:
            stmt = stmt.where(
                _tag_clause(tags, tag_match, lambda rec: rec.source_task_id == Task.id)
            )
        return stmt

    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        limit: int,
        status: TaskStatus | None = None,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        source_type: str | None = None,
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
    ) -> list[Task]:
        """Cursor page of a user's tasks within the open interval
        ``after < (created_at, id) < before`` (either bound optional), with an
        optional ``status`` filter.

        Ordered by the composite ``(created_at, id)`` key so equal
        created_at timestamps never cause a skipped or duplicated row.
        ``order`` selects which end ``limit`` cuts from: ``desc`` (newest
        first, for paging downward) or ``asc`` (oldest first, for pulling
        the tasks adjacent to a head cursor). ``status`` and the ``q`` /
        ``created_from`` / ``created_to`` / ``source_type`` filters only
        narrow the set — the cursor/order logic is unchanged.
        """
        key = tuple_(Task.created_at, Task.id)
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id)
        )
        if status is not None:
            stmt = stmt.where(Task.status == status)
        # Membership filters, used by the voice filters: those resolve to a set
        # of task ids in a separate query because identification lives in
        # match_decisions, which this statement does not join.
        if task_ids is not None:
            stmt = stmt.where(Task.id.in_(task_ids))
        if exclude_task_ids:
            stmt = stmt.where(Task.id.notin_(exclude_task_ids))
        stmt = self._apply_task_filters(
            stmt, q=q, created_from=created_from,
            created_to=created_to, source_type=source_type,
            tags=tags, tag_match=tag_match,
        )
        if before is not None:
            stmt = stmt.where(key < tuple_(*before))
        if after is not None:
            stmt = stmt.where(key > tuple_(*after))
        if order == "asc":
            stmt = stmt.order_by(Task.created_at.asc(), Task.id.asc())
        else:
            stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc())
        stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def count_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        status: TaskStatus | None = None,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        source_type: str | None = None,
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """How many of the user's tasks match these filters, ignoring paging.

        Deliberately shares ``_apply_task_filters`` with ``list_tasks_page`` so
        the count can never disagree with the list it labels: a second hand-written
        WHERE clause is exactly how a "showing 20 of 17" bug happens.
        No cursor and no limit — that is the point.
        """
        stmt = select(func.count()).select_from(Task).where(Task.user_id == user_id)
        # Every narrowing list_tasks_page applies must be applied here too, or
        # the count describes a different set than the page it labels — the
        # "showing 20 of 17" bug this method's own docstring warns about.
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if task_ids is not None:
            stmt = stmt.where(Task.id.in_(task_ids))
        if exclude_task_ids:
            stmt = stmt.where(Task.id.notin_(exclude_task_ids))
        stmt = self._apply_task_filters(
            stmt, q=q, created_from=created_from,
            created_to=created_to, source_type=source_type,
            tags=tags, tag_match=tag_match,
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def list_tasks_for_user_filtered(
        self,
        user_id: uuid.UUID,
        *,
        status: TaskStatus | None = None,
        limit: int = 20,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> list[Task]:
        """List tasks owned by user_id with optional status filter and explicit sort.

        sort: one of "created_at" | "updated_at" | "title" (where title sorts by source_title).
        order: "asc" | "desc".
        """
        sort_columns = {
            "created_at": Task.created_at,
            "updated_at": Task.updated_at,
            "title": Task.source_title,
        }
        column = sort_columns.get(sort)
        if column is None:
            raise ValueError(f"unsupported sort: {sort}")
        ordering = column.desc() if order == "desc" else column.asc()
        stmt = (
            select(Task)
            .where(Task.user_id == user_id)
        )
        if status is not None:
            stmt = stmt.where(Task.status == status)
        stmt = stmt.order_by(ordering).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_task_ids_for_statuses(self, statuses: list[TaskStatus]) -> list[uuid.UUID]:
        stmt = select(Task.id).where(Task.status.in_(statuses)).order_by(Task.created_at.asc(), Task.id.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def task_ids_in(self, task_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these ids exist as tasks, in ANY status (archived included).

        Used by the abandoned-upload sweep to prove a directory is not a real
        task's artifacts before deleting it.
        """
        if not task_ids:
            return set()
        result = await self.session.scalars(select(Task.id).where(Task.id.in_(task_ids)))
        return set(result.all())

    async def get_global_queue_positions(self) -> dict[uuid.UUID, int]:
        stmt = select(Task.id).where(Task.status == TaskStatus.queued).order_by(Task.created_at.asc(), Task.id.asc())
        result = await self.session.scalars(stmt)
        queued_ids = list(result.all())
        return {task_id: index for index, task_id in enumerate(queued_ids, start=1)}

    async def dequeue_task(self) -> uuid.UUID | None:
        """Atomically claim the oldest queued task. Returns its id or None."""
        stmt = (
            select(Task.id)
            .where(Task.status == TaskStatus.queued)
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task_id = await self.session.scalar(stmt)
        if task_id is None:
            return None
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=TaskStatus.running, updated_at=utcnow()),
        )
        await self.session.flush()
        return task_id

    async def set_task_status_by_id(self, task_id: uuid.UUID, status: TaskStatus) -> None:
        """Update task status by id without loading the full task object."""
        await self.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=status, updated_at=utcnow()),
        )
        await self._sync_recording_state(task_id, status)
        await self.session.flush()

    async def _sync_recording_state(self, task_id: uuid.UUID, status: TaskStatus) -> None:
        """Move the task's recording to the state this status implies (vts-lzad).

        No-op for statuses with no mapping (paused/waiting keep what the recording
        had; archived only closes a still-processing one) and when the task has no recording (never created, or deleted).
        """
        state = state_for_status(status)
        if status == TaskStatus.archived:
            # Archived has no state of its own (the mapping stays pure), but a
            # task archived from awaiting_input never gets another status, so
            # its recording would stay `processing` forever. Finished states
            # are left alone.
            await self.session.execute(
                update(Recording)
                .where(Recording.source_task_id == task_id, Recording.state == RecordingState.processing)
                .values(state=RecordingState.incomplete)
                .execution_options(synchronize_session="fetch")
            )
            return
        if state is None:
            return
        await self.session.execute(
            update(Recording)
            .where(Recording.source_task_id == task_id)
            .values(state=state)
            .execution_options(synchronize_session="fetch")
        )

    async def transition_task_status(
        self, task_id: uuid.UUID, from_statuses: list[TaskStatus], to_status: TaskStatus
    ) -> bool:
        """Conditional status UPDATE; returns True iff a row changed.

        Used as a race guard: only flips status when the task is still in one
        of `from_statuses`, so a task the API just canceled/paused is never
        overwritten to waiting/running.
        """
        result = await self.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.in_(from_statuses))
            .values(status=to_status, updated_at=utcnow())
        )
        await self.session.flush()
        return bool(result.rowcount)

    async def requeue_running_tasks(self) -> list[uuid.UUID]:
        # "Active" set (running/waiting) for recovery/requeue on startup.
        stmt = select(Task).where(Task.status.in_(list(task_status.ACTIVE_STATUSES)))
        result = await self.session.scalars(stmt)
        tasks = list(result.all())
        for task in tasks:
            task.status = TaskStatus.queued
            task.error_message = None
            task.updated_at = utcnow()
        await self.session.flush()
        return [task.id for task in tasks]

    async def get_tasks_for_user(
        self, user_id: uuid.UUID, task_ids: list[uuid.UUID], *, load_steps: bool = False
    ) -> list[Task]:
        stmt = select(Task).where(Task.user_id == user_id, Task.id.in_(task_ids))
        if load_steps:
            stmt = stmt.options(selectinload(Task.steps))
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_task_for_user(
        self, user_id: uuid.UUID, task_id: uuid.UUID, *, lock: bool = False
    ) -> Task | None:
        """The task, if it belongs to `user_id`.

        `lock=True` takes a row lock (`SELECT … FOR UPDATE`) held until this
        session's next commit, so two requests editing the same task run one
        after the other instead of interleaving. Only the task row is locked —
        the eager-loaded steps are read normally.
        """
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id, Task.id == task_id)
        )
        if lock:
            stmt = stmt.with_for_update(of=Task)
        return await self.session.scalar(stmt)

    async def get_task_by_id(self, task_id: uuid.UUID) -> Task | None:
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.id == task_id)
        )
        return await self.session.scalar(stmt)

    async def set_task_status(
        self, task: Task, status: TaskStatus, error_message: str | None = None
    ) -> None:
        task.status = status
        task.error_message = error_message
        # The awaited step describes a wait, so it ends with the wait (vts-47w6).
        # It used to be written by set_awaiting_input and cleared by nobody, so a
        # task that resumed and finished kept reporting the step it had once
        # paused at — the API then served `completed` alongside
        # `awaiting_step=match_speakers`. The SPA never showed it because every
        # read pairs the step with the status, but the serialized task is a
        # contract other clients read too.
        #
        # set_awaiting_input does not go through here, so re-entering a wait
        # still sets the step normally.
        task.awaiting_step = None
        task.updated_at = utcnow()
        await self.session.flush()
        await self._sync_recording_state(task.id, status)

    async def set_awaiting_input(self, task: Task, step: str) -> None:
        task.status = TaskStatus.awaiting_input
        task.awaiting_step = step
        task.updated_at = utcnow()
        await self.session.flush()

    async def set_task_summary_progress(self, task: Task, current: int, total: int) -> None:
        task.summary_progress = {"current": current, "total": total}
        task.updated_at = utcnow()
        await self.session.flush()

    async def upsert_step(self, task_id: uuid.UUID, name: str) -> Step:
        stmt = select(Step).where(Step.task_id == task_id, Step.name == name)
        step = await self.session.scalar(stmt)
        if step:
            return step
        step = Step(task_id=task_id, name=name, status=StepStatus.pending)
        self.session.add(step)
        await self.session.flush()
        return step

    async def delete_steps_by_name(self, task_id: uuid.UUID, names: list[str]) -> int:
        if not names:
            return 0
        stmt = select(Step).where(Step.task_id == task_id, Step.name.in_(names))
        rows = list(await self.session.scalars(stmt))
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        return len(rows)

    async def set_step_status(
        self,
        step: Step,
        status: StepStatus,
        message: str | None = None,
    ) -> None:
        now = utcnow()
        if status == StepStatus.running:
            step.started_at = now
            step.attempt += 1
        if status in {StepStatus.completed, StepStatus.failed, StepStatus.skipped}:
            step.finished_at = now
        step.status = status
        step.message = message
        await self.session.flush()

    async def has_segment(self, task_id: uuid.UUID, segment_index: int) -> bool:
        stmt = select(AsrSegment.id).where(
            AsrSegment.task_id == task_id, AsrSegment.segment_index == segment_index
        )
        row = await self.session.scalar(stmt)
        return row is not None

    async def add_asr_segment(
        self,
        task_id: uuid.UUID,
        segment_index: int,
        start_sec: float,
        end_sec: float,
        text: str,
        raw_json: dict[str, object],
    ) -> AsrSegment:
        segment = AsrSegment(
            task_id=task_id,
            segment_index=segment_index,
            start_sec=start_sec,
            end_sec=end_sec,
            text=text,
            raw_json=raw_json,
            payload=decompose_raw_json(raw_json),
        )
        self.session.add(segment)
        await self.session.flush()
        return segment

    async def upsert_recording_for_task(self, task: Task) -> "Recording":
        """The recording this task produces, created on first call and updated
        after (vts-8w1r).

        A task creates OR UPDATES its recording; re-running one must not
        accumulate copies, which is what the partial unique index on
        source_task_id enforces at the database level.

        `duration_sec` comes from the last ASR segment's end rather than from
        probing the media file: the ASR covers the whole recording, and unlike
        the media (and the probe sidecar sitting next to it) the segments
        survive archiving. `language` follows effective_language's order —
        an explicit choice first, detection second.
        """
        recording = await self.session.scalar(
            select(Recording).where(Recording.source_task_id == task.id)
        )
        duration = await self.session.scalar(
            select(func.max(AsrSegment.end_sec)).where(AsrSegment.task_id == task.id)
        )
        options = task.options if isinstance(task.options, dict) else {}
        # Normalised, because the pipeline stores whichever spelling its backend
        # produced — "ru" from the ASR sidecar, "russian" from cpp — and a
        # library listing both as separate languages is wrong on its face.
        language = language_code(options.get("language") or options.get("detected_language"))

        if recording is None:
            recording = Recording(
                user_id=task.user_id,
                source_task_id=task.id,
                artifact_dir=task.artifact_dir,
                recorded_at=task.created_at,
                # Created here only when the recording went missing (deleted,
                # or a task older than the recording table): it holds no
                # content yet, and that is what its state is judged from.
                state=initial_state(
                    task.status,
                    transcript_path=None,
                    summary_path=None,
                    prompt_results=[],
                    options=options,
                ),
            )
            self.session.add(recording)

        # A name the user gave the RECORDING is not the task's to overwrite —
        # and this runs on every re-index and at completion, so without the
        # check a chosen name would not survive the next pipeline pass.
        if not recording.title_is_custom:
            recording.title = recording_display_name(task.source_title, task.source_url)
        recording.source_url = task.source_url
        # Content (transcript, summary, prompt results) is not copied: the
        # pipeline writes it to the recording directly (vts-lzad).
        # Never overwrite a known length or language with nothing: the media may
        # already be archived by the time this runs again.
        if duration is not None:
            recording.duration_sec = float(duration)
        if language:
            recording.language = language
        await self.session.flush()
        return recording

    def _apply_recording_filters(
        self,
        stmt: Any,
        *,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
    ) -> Any:
        """The one place recording filters are expressed.

        Shared by the page query and the count so the two can never disagree,
        and shared by the HTTP endpoint and the MCP tool so a filter added to
        one is not missing from the other — three separate WHERE clauses is
        how the library search came to look only at the loaded page.

        A recording that has never been listed (no text yet) is neither listed
        nor counted: there is nothing in it to open (vts-lzad). It stays
        reachable by id through `get_recording_for_user`, which is
        deliberately not filtered.

        `before` / `after` are strict keyset bounds on `(listed_at, id)`, the
        library order; `count_recordings` never passes them.
        """
        stmt = stmt.where(Recording.listed_at.isnot(None))
        key = tuple_(Recording.listed_at, Recording.id)
        if before is not None:
            stmt = stmt.where(key < tuple_(*before))
        if after is not None:
            stmt = stmt.where(key > tuple_(*after))
        if q:
            stmt = stmt.where(Recording.title.ilike(f"%{q.strip()}%"))
        if created_from is not None:
            stmt = stmt.where(Recording.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(Recording.created_at <= created_to)
        if task_ids is not None:
            stmt = stmt.where(Recording.source_task_id.in_(task_ids))
        if exclude_task_ids:
            stmt = stmt.where(
                or_(
                    Recording.source_task_id.is_(None),
                    Recording.source_task_id.notin_(exclude_task_ids),
                )
            )
        if tags:
            stmt = stmt.where(
                _tag_clause(tags, tag_match, lambda rec: rec.id == Recording.id)
            )
        return stmt

    async def list_recordings(
        self,
        user_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
        *,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
    ) -> list["Recording"]:
        """A user's recordings in library order (`listed_at`, newest first by
        default), narrowed in the QUERY. `before` / `after` are keyset cursors;
        `order="asc"` pulls the rows next to an `after` cursor.

        Filtering here rather than in the caller is the whole point: a client
        that filters the page it happens to hold reports "not found" for a row
        on the next page.
        """
        stmt = self._apply_recording_filters(
            select(Recording).where(Recording.user_id == user_id),
            q=q, created_from=created_from, created_to=created_to,
            task_ids=task_ids, exclude_task_ids=exclude_task_ids,
            before=before, after=after, tags=tags, tag_match=tag_match,
        )
        if order == "asc":
            ordering = (Recording.listed_at.asc(), Recording.id.asc())
        else:
            ordering = (Recording.listed_at.desc(), Recording.id.desc())
        stmt = stmt.order_by(*ordering).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_recordings(
        self,
        user_id: uuid.UUID,
        *,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        task_ids: list[uuid.UUID] | None = None,
        exclude_task_ids: list[uuid.UUID] | None = None,
        tags: list[str] | None = None,
        tag_match: Literal["all", "any"] = "all",
    ) -> int:
        """How many recordings match, ignoring paging.

        Counting the corpus while listing a filtered page produces "showing 1
        of 3", which is why this takes the same arguments.
        """
        stmt = self._apply_recording_filters(
            select(func.count()).select_from(Recording).where(
                Recording.user_id == user_id
            ),
            q=q, created_from=created_from, created_to=created_to,
            task_ids=task_ids, exclude_task_ids=exclude_task_ids,
            tags=tags, tag_match=tag_match,
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def get_recording_for_user(
        self, user_id: uuid.UUID, recording_id: uuid.UUID
    ) -> "Recording | None":
        return await self.session.scalar(
            select(Recording).where(
                Recording.id == recording_id, Recording.user_id == user_id
            )
        )

    async def get_task_segment_by_index(self, task_id: uuid.UUID, segment_index: int) -> AsrSegment | None:
        stmt = select(AsrSegment).where(
            AsrSegment.task_id == task_id,
            AsrSegment.segment_index == segment_index,
        )
        return await self.session.scalar(stmt)

    async def upsert_asr_segment_payload(
        self,
        *,
        task_id: uuid.UUID,
        segment_index: int,
        start_sec: float,
        end_sec: float,
        text: str,
        raw_json: dict[str, object],
    ) -> AsrSegment:
        segment = await self.get_task_segment_by_index(task_id, segment_index)
        if segment is None:
            return await self.add_asr_segment(
                task_id=task_id,
                segment_index=segment_index,
                start_sec=start_sec,
                end_sec=end_sec,
                text=text,
                raw_json=raw_json,
            )
        segment.start_sec = start_sec
        segment.end_sec = end_sec
        segment.text = text
        segment.raw_json = raw_json
        segment.payload = decompose_raw_json(raw_json)
        await self.session.flush()
        return segment

    async def get_task_segments(self, task_id: uuid.UUID) -> list[AsrSegment]:
        stmt = select(AsrSegment).where(AsrSegment.task_id == task_id).order_by(AsrSegment.segment_index.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_asr_progress_for_tasks(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        if not task_ids:
            return {}
        # Count in SQL. Selecting raw_json to test it for emptiness pulled every
        # ASR payload (word-level timings) across the wire for a task-list page:
        # measured at 25 tasks x 400 segments, ~8.6MB and ~250ms just to produce
        # two integers per task (vts-7q7).
        #
        # raw_json is a `json` column, not `jsonb`, and `json` has no equality
        # operator in Postgres — so emptiness is tested on the text form. The
        # old Python check was `isinstance(raw_json, dict) and bool(raw_json)`,
        # i.e. a non-empty JSON object; '{}' and SQL NULL are the two ways to
        # be not-done, and both are excluded here.
        done_filter = func.count().filter(
            func.cast(AsrSegment.raw_json, Text).notin_(("{}", "null"))
        )
        stmt = (
            select(AsrSegment.task_id, done_filter, func.count())
            .where(AsrSegment.task_id.in_(task_ids))
            .group_by(AsrSegment.task_id)
        )
        result = await self.session.execute(stmt)
        return {task_id: (done, total) for task_id, done, total in result.all()}

    async def clear_asr_for_task(self, task_id: uuid.UUID) -> None:
        await self.session.execute(delete(AsrSegment).where(AsrSegment.task_id == task_id))
        await self.session.flush()

    async def find_completed_donor(
        self,
        source_url: str,
        options: dict,
        exclude_user_id: uuid.UUID,
    ) -> Task | None:
        """Find a completed task from another user with the same source_url and options."""
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(
                Task.source_url == source_url,
                Task.status == TaskStatus.completed,
                Task.user_id != exclude_user_id,
            )
            .order_by(Task.updated_at.desc())
            .limit(_DONOR_CANDIDATE_LIMIT)
        )
        # Scan candidates newest-first instead of testing only the newest row.
        # With limit(1), a newer task for the same URL with DIFFERENT options
        # shadowed an older exact match, and donor-clone silently fell back to
        # full processing — a wasted GPU/LLM pass, not a correctness bug
        # (vts-wpy).
        #
        # Compared in Python rather than in the WHERE clause because `options`
        # is a JSON column: Postgres would compare it as jsonb/text, where key
        # order and numeric spelling (1 vs 1.0) decide equality, so a
        # semantically identical dict could miss. The candidate list is capped
        # so this stays one bounded query.
        for candidate in (await self.session.scalars(stmt)).all():
            if candidate.options == options:
                return candidate
        return None

    async def create_api_token(
        self,
        user_id: uuid.UUID,
        name: str,
        token_hash: str,
        prefix: str,
    ) -> ApiToken:
        token = ApiToken(user_id=user_id, name=name, token_hash=token_hash, prefix=prefix)
        self.session.add(token)
        await self.session.flush()
        return token

    async def list_api_tokens(self, user_id: uuid.UUID) -> list[ApiToken]:
        stmt = (
            select(ApiToken)
            .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_active_api_token_by_hash(self, token_hash: str) -> ApiToken | None:
        stmt = select(ApiToken).where(
            ApiToken.token_hash == token_hash, ApiToken.revoked_at.is_(None)
        )
        return await self.session.scalar(stmt)

    async def revoke_api_token(self, user_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        stmt = select(ApiToken).where(
            ApiToken.id == token_id,
            ApiToken.user_id == user_id,
            ApiToken.revoked_at.is_(None),
        )
        token = await self.session.scalar(stmt)
        if token is None:
            return False
        token.revoked_at = utcnow()
        await self.session.flush()
        return True

    async def touch_api_token_last_used(self, token_id: uuid.UUID) -> None:
        stmt = (
            update(ApiToken)
            .where(ApiToken.id == token_id)
            .values(last_used_at=utcnow())
        )
        await self.session.execute(stmt)

    async def clone_asr_segments(self, src_task_id: uuid.UUID, dst_task_id: uuid.UUID) -> None:
        """Copy all ASR segments from src task to dst task."""
        stmt = select(AsrSegment).where(AsrSegment.task_id == src_task_id).order_by(AsrSegment.segment_index.asc())
        result = await self.session.scalars(stmt)
        segments = list(result.all())
        for seg in segments:
            new_seg = AsrSegment(
                task_id=dst_task_id,
                segment_index=seg.segment_index,
                start_sec=seg.start_sec,
                end_sec=seg.end_sec,
                text=seg.text,
                raw_json=seg.raw_json,
            )
            self.session.add(new_seg)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Prompt CRUD
    # ------------------------------------------------------------------

    async def create_prompt(
        self,
        user_id: uuid.UUID,
        name: str,
        system_prompt: str,
        *,
        is_system: bool = False,
    ) -> Prompt:
        prompt = Prompt(
            user_id=user_id,
            name=name,
            system_prompt=system_prompt,
            is_system=is_system,
            created_at=utcnow(),
            # A prompt the user wrote is edited by definition; a vendor copy
            # has not been touched yet, and NULL is what says so.
            updated_at=None if is_system else utcnow(),
        )
        self.session.add(prompt)
        await self.session.flush()
        return prompt

    async def list_prompts(
        self, user_id: uuid.UUID, *, include_system: bool = True
    ) -> list[Prompt]:
        """This user's prompts, vendor copy included by default.

        The vendor prompt is not a separate kind of thing: it is a row in the
        user's own table flagged `is_system` (see Prompt.is_system). Callers
        that already list the built-in prompts from the registry must pass
        `include_system=False`, or the same artifact appears twice under two
        names — once as the built-in and once as the user's copy of it.
        """
        stmt = (
            select(Prompt)
            .where(Prompt.user_id == user_id)
            .order_by(Prompt.created_at.desc())
        )
        if not include_system:
            stmt = stmt.where(Prompt.is_system.is_(False))
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> Prompt | None:
        stmt = select(Prompt).where(Prompt.id == prompt_id, Prompt.user_id == user_id)
        return await self.session.scalar(stmt)

    async def update_prompt(
        self,
        user_id: uuid.UUID,
        prompt_id: uuid.UUID,
        *,
        name: str | None,
        system_prompt: str | None,
    ) -> Prompt | None:
        prompt = await self.get_prompt(user_id, prompt_id)
        if prompt is None:
            return None
        if name is not None:
            prompt.name = name
        if system_prompt is not None:
            prompt.system_prompt = system_prompt
        prompt.updated_at = utcnow()
        await self.session.flush()
        return prompt

    async def delete_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> bool:
        prompt = await self.get_prompt(user_id, prompt_id)
        if prompt is None:
            return False
        await self.session.delete(prompt)
        await self.session.flush()
        return True

    async def set_task_prompt_results(self, task: Task, prompt_results: list[dict]) -> None:
        """Replace the prompt-result index of the task's recording.

        Copies the entries, so later changes to the caller's dicts do not leak
        in, and reassigns the JSON column rather than mutating it.
        """
        task.updated_at = utcnow()
        recording = await self.recording_for_task(task.id)
        if recording is not None:
            recording.prompt_results = [dict(e) for e in prompt_results if isinstance(e, dict)]
        await self.session.flush()

    # ------------------------------------------------------------------
    # Recording content (vts-lzad)
    #
    # The recording owns its content fields and is the only place they are
    # written (stage 3). A task without a recording (deleted mid-run) has
    # nowhere to put content: these setters never raise and never create one.
    # ------------------------------------------------------------------

    async def recording_for_task(self, task_id: uuid.UUID) -> Recording | None:
        return await self.session.scalar(
            select(Recording).where(Recording.source_task_id == task_id)
        )

    async def recordings_for_tasks(
        self, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Recording]:
        """The recordings of a page of tasks, keyed by task id — one query."""
        if not task_ids:
            return {}
        rows = await self.session.scalars(
            select(Recording).where(Recording.source_task_id.in_(task_ids))
        )
        return {r.source_task_id: r for r in rows}

    @staticmethod
    def _mark_listed(recording: "Recording") -> None:
        """Stamp `listed_at` the first time the recording has text; never again."""
        if recording.transcript_path is not None and recording.listed_at is None:
            recording.listed_at = utcnow()

    async def set_task_content(
        self,
        task: Task,
        *,
        transcript_path: str | None | object = UNSET,
        summary_path: str | None | object = UNSET,
        redacted_path: str | None | object = UNSET,
    ) -> None:
        """Write the given content paths to the task's recording.

        Fields left at UNSET are untouched; an explicit None clears.
        """
        recording = await self.recording_for_task(task.id)
        if recording is not None:
            if transcript_path is not UNSET:
                recording.transcript_path = transcript_path
                self._mark_listed(recording)
            if summary_path is not UNSET:
                recording.summary_path = summary_path
            if redacted_path is not UNSET:
                recording.redacted_path = redacted_path
        task.updated_at = utcnow()
        await self.session.flush()

    async def set_detected_language(self, task: Task, language: str, confidence: float) -> None:
        """The detection is the pipeline's runtime input (task options) and
        the recording's language (normalised code)."""
        new_options = dict(task.options or {})
        new_options["detected_language"] = language
        new_options["detected_language_confidence"] = float(confidence)
        task.options = new_options  # reassigned, never mutated in place
        task.updated_at = utcnow()
        code = language_code(language)
        if code:
            recording = await self.recording_for_task(task.id)
            if recording is not None:
                recording.language = code
        await self.session.flush()

    # ------------------------------------------------------------------
    # Preset CRUD
    # ------------------------------------------------------------------

    async def create_preset(self, user_id: uuid.UUID, name: str, options: dict) -> Preset:
        preset = Preset(user_id=user_id, name=name, options=options)
        self.session.add(preset)
        await self.session.flush()
        return preset

    async def list_presets(self, user_id: uuid.UUID) -> list[Preset]:
        stmt = select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at.desc())
        return list(await self.session.scalars(stmt))

    async def get_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> Preset | None:
        return await self.session.scalar(
            select(Preset).where(Preset.id == preset_id, Preset.user_id == user_id))

    async def update_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID, *, name: str | None, options: dict | None) -> Preset | None:
        preset = await self.get_preset(user_id, preset_id)
        if preset is None:
            return None
        if name is not None:
            preset.name = name
        if options is not None:
            preset.options = options
        await self.session.flush()
        return preset

    async def get_user_default_preset(self, user_id: uuid.UUID) -> dict | None:
        u = await self.session.scalar(select(User).where(User.id == user_id))
        return u.default_preset if u else None

    async def set_user_default_preset(self, user_id: uuid.UUID, ref: dict | None) -> None:
        u = await self.session.scalar(select(User).where(User.id == user_id))
        if u is not None:
            u.default_preset = ref
            await self.session.flush()

    async def delete_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> bool:
        preset = await self.get_preset(user_id, preset_id)
        if preset is None:
            return False
        u = await self.session.scalar(select(User).where(User.id == user_id))
        if u is not None and u.default_preset == {"source": "user", "id": str(preset_id)}:
            u.default_preset = None
        await self.session.delete(preset)
        await self.session.flush()
        return True

    # ------------------------------------------------------------------
    # DeliveryTarget CRUD
    # ------------------------------------------------------------------

    async def create_delivery_credential(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        config: dict, secrets_enc: bytes | None,
    ) -> DeliveryCredential:
        credential = DeliveryCredential(
            user_id=user_id, name=name, adapter=adapter,
            config_json=config, secrets_enc=secrets_enc)
        self.session.add(credential)
        await self.session.flush()
        return credential

    async def list_delivery_credentials(self, user_id: uuid.UUID) -> list[DeliveryCredential]:
        stmt = (select(DeliveryCredential)
                .where(DeliveryCredential.user_id == user_id)
                .order_by(DeliveryCredential.created_at.desc()))
        return list(await self.session.scalars(stmt))

    async def get_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> DeliveryCredential | None:
        return await self.session.scalar(
            select(DeliveryCredential).where(
                DeliveryCredential.id == credential_id,
                DeliveryCredential.user_id == user_id))

    async def get_delivery_credential_by_name(
        self, user_id: uuid.UUID, name: str
    ) -> DeliveryCredential | None:
        return await self.session.scalar(
            select(DeliveryCredential).where(
                DeliveryCredential.user_id == user_id,
                DeliveryCredential.name == name))

    async def count_targets_for_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> int:
        """How many targets hang off this credential.

        Surfaced to callers so deleting a credential in use can be refused
        with a count instead of an opaque foreign-key error.
        """
        stmt = (select(func.count())
                .select_from(DeliveryTarget)
                .where(DeliveryTarget.user_id == user_id,
                       DeliveryTarget.credential_id == credential_id))
        return int(await self.session.scalar(stmt) or 0)

    async def update_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        secrets_enc: bytes | None, clear_secrets: bool,
    ) -> DeliveryCredential | None:
        credential = await self.get_delivery_credential(user_id, credential_id)
        if credential is None:
            return None
        if name is not None:
            credential.name = name
        if config is not None:
            credential.config_json = config
        if clear_secrets:
            credential.secrets_enc = None
        elif secrets_enc is not None:
            credential.secrets_enc = secrets_enc
        await self.session.flush()
        return credential

    async def delete_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> bool:
        credential = await self.get_delivery_credential(user_id, credential_id)
        if credential is None:
            return False
        await self.session.delete(credential)
        await self.session.flush()
        return True

    async def create_delivery_target(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        credential_id: uuid.UUID, config: dict,
    ) -> DeliveryTarget:
        target = DeliveryTarget(
            user_id=user_id, name=name, adapter=adapter,
            credential_id=credential_id, config_json=config)
        self.session.add(target)
        await self.session.flush()
        return target

    async def list_delivery_targets(self, user_id: uuid.UUID) -> list[DeliveryTarget]:
        stmt = (select(DeliveryTarget)
                .where(DeliveryTarget.user_id == user_id)
                .order_by(DeliveryTarget.created_at.desc()))
        return list(await self.session.scalars(stmt))

    async def get_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> DeliveryTarget | None:
        return await self.session.scalar(
            select(DeliveryTarget).where(
                DeliveryTarget.id == target_id, DeliveryTarget.user_id == user_id))

    async def get_delivery_target_by_name(self, user_id: uuid.UUID, name: str) -> DeliveryTarget | None:
        return await self.session.scalar(
            select(DeliveryTarget).where(
                DeliveryTarget.user_id == user_id, DeliveryTarget.name == name))

    async def update_delivery_target(
        self, user_id: uuid.UUID, target_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        credential_id: uuid.UUID | None = None,
    ) -> DeliveryTarget | None:
        target = await self.get_delivery_target(user_id, target_id)
        if target is None:
            return None
        if name is not None:
            target.name = name
        if config is not None:
            target.config_json = config
        if credential_id is not None:
            target.credential_id = credential_id
        await self.session.flush()
        return target

    async def delete_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> bool:
        target = await self.get_delivery_target(user_id, target_id)
        if target is None:
            return False
        await self.session.delete(target)
        await self.session.flush()
        return True

    # ------------------------------------------------------------------
    # DeliveryAttempt
    # ------------------------------------------------------------------

    async def create_delivery_attempt(
        self, *, task_id: uuid.UUID, target_id: uuid.UUID | None,
        adapter: str, variant: str, max_attempts: int,
        next_attempt_at: datetime,
    ) -> DeliveryAttempt:
        row = DeliveryAttempt(
            task_id=task_id, target_id=target_id, adapter=adapter, variant=variant,
            status=DeliveryStatus.pending, max_attempts=max_attempts,
            next_attempt_at=next_attempt_at)
        self.session.add(row)
        await self.session.flush()
        return row

    async def claim_due_deliveries(self, now: datetime, limit: int) -> list[DeliveryAttempt]:
        # waiting_adapter rows are claimed too: a parked delivery must wake up and
        # retry once its plugin is back, otherwise it would sit there forever.
        stmt = (select(DeliveryAttempt)
                .where(DeliveryAttempt.status.in_(
                           [DeliveryStatus.pending, DeliveryStatus.waiting_adapter]),
                       DeliveryAttempt.next_attempt_at <= now)
                .order_by(DeliveryAttempt.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True))
        rows = list(await self.session.scalars(stmt))
        for row in rows:
            row.status = DeliveryStatus.delivering
            row.attempts += 1
        await self.session.flush()
        return rows

    async def record_delivery_result(
        self, attempt_id: uuid.UUID, *, external_id: str | None, external_url: str | None
    ) -> None:
        row = await self.session.get(DeliveryAttempt, attempt_id)
        if row is None:
            return
        row.status = DeliveryStatus.delivered
        row.external_id = external_id
        row.external_url = external_url
        row.last_error = None
        await self.session.flush()

    async def record_delivery_failure(
        self, attempt_id: uuid.UUID, *, last_error: str,
        next_attempt_at: datetime | None, dead: bool,
    ) -> None:
        row = await self.session.get(DeliveryAttempt, attempt_id)
        if row is None:
            return
        row.status = DeliveryStatus.dead if dead else DeliveryStatus.pending
        row.last_error = last_error[:2000]
        row.next_attempt_at = next_attempt_at
        await self.session.flush()

    async def park_delivery_for_adapter(
        self, attempt_id: uuid.UUID, *, next_attempt_at: datetime
    ) -> None:
        """Park a delivery whose adapter is not loaded right now.

        Not a failure: the attempt claim() charged is refunded, so a missing
        plugin can never exhaust max_attempts and kill the delivery. The row
        keeps its intent and leaves on its own once the adapter is back.
        """
        row = await self.session.get(DeliveryAttempt, attempt_id)
        if row is None:
            return
        row.status = DeliveryStatus.waiting_adapter
        row.attempts = max(0, row.attempts - 1)
        row.next_attempt_at = next_attempt_at
        await self.session.flush()

    async def list_deliveries_for_task(self, task_id: uuid.UUID) -> list[DeliveryAttempt]:
        stmt = (select(DeliveryAttempt)
                .where(DeliveryAttempt.task_id == task_id)
                .order_by(DeliveryAttempt.created_at))
        return list(await self.session.scalars(stmt))

    async def reap_stuck_deliveries(self, older_than: datetime) -> int:
        stmt = (update(DeliveryAttempt)
                .where(DeliveryAttempt.status == DeliveryStatus.delivering,
                       DeliveryAttempt.updated_at < older_than)
                .values(status=DeliveryStatus.pending))
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def reset_delivery_for_retry(
        self, task_id: uuid.UUID, target_id: uuid.UUID | None, now: datetime
    ) -> int:
        # RETRYABLE_STATUSES is the single source of "what retry revives" — it
        # deliberately excludes waiting_adapter, which is not stuck but waiting.
        conds = [DeliveryAttempt.task_id == task_id,
                 DeliveryAttempt.status.in_(sorted(RETRYABLE_STATUSES))]
        if target_id is not None:
            conds.append(DeliveryAttempt.target_id == target_id)
        stmt = (update(DeliveryAttempt).where(*conds)
                .values(status=DeliveryStatus.pending, next_attempt_at=now))
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Speaker registry CRUD
    # ------------------------------------------------------------------

    async def create_speaker(self, user_id: uuid.UUID, name: str) -> Speaker:
        speaker = Speaker(user_id=user_id, name=name)
        self.session.add(speaker)
        await self.session.flush()
        return speaker

    async def list_speakers(self, user_id: uuid.UUID) -> list[Speaker]:
        stmt = select(Speaker).where(Speaker.user_id == user_id).order_by(Speaker.name.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID) -> Speaker | None:
        stmt = select(Speaker).where(Speaker.id == speaker_id, Speaker.user_id == user_id)
        return await self.session.scalar(stmt)

    async def rename_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID, name: str) -> Speaker | None:
        speaker = await self.get_speaker(user_id, speaker_id)
        if speaker is None:
            return None
        speaker.name = name
        await self.session.flush()
        return speaker

    async def delete_speaker(self, user_id: uuid.UUID, speaker_id: uuid.UUID) -> bool:
        speaker = await self.get_speaker(user_id, speaker_id)
        if speaker is None:
            return False
        await self.session.delete(speaker)
        await self.session.flush()
        return True

    async def add_voice_sample(
        self, *, speaker_id: uuid.UUID, embedding: list[float], embedding_model: str,
        audio: bytes, audio_format: str, duration_sec: float,
        source_task_id: uuid.UUID | None,
    ) -> VoiceSample:
        sample = VoiceSample(
            speaker_id=speaker_id, embedding=embedding, embedding_model=embedding_model,
            audio=audio, audio_format=audio_format, duration_sec=duration_sec,
            source_task_id=source_task_id,
        )
        self.session.add(sample)
        await self.session.flush()
        return sample

    async def nearest_voice_sample(
        self, speaker_id: uuid.UUID, embedding: list[float], embedding_model: str,
    ) -> tuple[VoiceSample, float] | None:
        """The speaker's fragment closest to `embedding`, with its cosine distance.

        Only fragments of the same embedding model compare; None when the
        speaker has none.
        """
        dist = VoiceSample.embedding.cosine_distance(embedding).label("dist")
        stmt = (
            select(VoiceSample, dist)
            .where(VoiceSample.speaker_id == speaker_id, VoiceSample.embedding_model == embedding_model)
            .order_by(dist.asc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).first()
        return (row[0], float(row[1])) if row is not None else None

    async def list_voice_samples(self, speaker_id: uuid.UUID) -> list[VoiceSample]:
        # audio stays deferred — never loaded here.
        #
        # Newest first (vts-4nx): the registry dialog renders this order, and a
        # person's fragments accumulate over time, so oldest-first buried the
        # freshly recorded one at the bottom of a growing list. Ordered here
        # rather than in the browser so every consumer sees the same order.
        # id is the tiebreaker: samples added in one transaction share a
        # created_at, and without it their relative order is arbitrary.
        stmt = (
            select(VoiceSample)
            .where(VoiceSample.speaker_id == speaker_id)
            .order_by(VoiceSample.created_at.desc(), VoiceSample.id.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_voice_sample(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> VoiceSample | None:
        stmt = (
            select(VoiceSample)
            .join(Speaker, VoiceSample.speaker_id == Speaker.id)
            .where(VoiceSample.id == sample_id, Speaker.user_id == user_id)
        )
        return await self.session.scalar(stmt)

    async def delete_voice_sample(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> bool:
        sample = await self.get_voice_sample(user_id, sample_id)
        if sample is None:
            return False
        await self.session.delete(sample)
        await self.session.flush()
        return True

    async def reassign_speaker_samples(
        self, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID,
    ) -> int:
        """Move all of source's voice samples to target. Both must be the user's.

        Returns the number of samples reassigned. Does not touch MatchDecision —
        callers that need decision rewriting (merge) do it separately.
        """
        source = await self.get_speaker(user_id, source_id)
        target = await self.get_speaker(user_id, target_id)
        if source is None or target is None:
            return 0
        result = await self.session.execute(
            update(VoiceSample)
            .where(VoiceSample.speaker_id == source_id)
            .values(speaker_id=target_id)
        )
        await self.session.flush()
        return result.rowcount or 0

    async def move_voice_sample(
        self, user_id: uuid.UUID, sample_id: uuid.UUID, target_speaker_id: uuid.UUID,
    ) -> VoiceSample | None:
        """Reassign one sample to another of the user's speakers. None if not found.

        Deliberately leaves MatchDecision alone: a move says "this fragment was
        filed under the wrong person", not "that past match was wrong", so the
        calibration history must keep pointing where it did.
        """
        sample = await self.get_voice_sample(user_id, sample_id)
        target = await self.get_speaker(user_id, target_speaker_id)
        if sample is None or target is None:
            return None
        sample.speaker_id = target_speaker_id
        await self.session.flush()
        return sample

    async def move_candidates_for_sample(
        self, user_id: uuid.UUID, sample_id: uuid.UUID, limit: int | None = None,
    ) -> list[tuple[Speaker, float | None]]:
        """Destinations for moving `sample_id`, nearest first.

        Every OTHER speaker of this user is a candidate — the current owner is
        excluded (moving a fragment to where it already is means nothing), and a
        speaker with no comparable fragment still appears with distance None
        rather than dropping out: an empty person is a perfectly valid
        destination, and hiding it would make the fragment unmovable there.

        Distance is MIN cosine over the candidate's fragments computed by the
        SAME embedding model as this sample — distances across models are
        meaningless. Ordered NULLS LAST so rankable candidates come first.
        """
        sample = await self.get_voice_sample(user_id, sample_id)
        if sample is None:
            return []
        dist = func.min(
            VoiceSample.embedding.cosine_distance(sample.embedding)
        ).label("dist")
        stmt = (
            select(Speaker, dist)
            .outerjoin(
                VoiceSample,
                (VoiceSample.speaker_id == Speaker.id)
                & (VoiceSample.embedding_model == sample.embedding_model)
                & (VoiceSample.id != sample_id),
            )
            .where(Speaker.user_id == user_id, Speaker.id != sample.speaker_id)
            .group_by(Speaker.id)
            .order_by(dist.asc().nullslast(), Speaker.name.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self.session.execute(stmt)
        return [(row[0], None if row[1] is None else float(row[1])) for row in rows.all()]

    async def speaker_names_for_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> dict[str, str]:
        """Map speaker_label -> current Speaker.name for this task's matched voices.

        Joining decisions to speakers means a deleted person (speaker_id SET NULL,
        or the row gone) simply drops out — the caller renders "Голос N" for absent
        labels. Ordered ascending so the latest decision per label wins; id breaks
        ties between decisions written in the same transaction.
        """
        stmt = (
            select(MatchDecision.speaker_label, Speaker.name)
            .join(Speaker, MatchDecision.speaker_id == Speaker.id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == task_id,
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        rows = await self.session.execute(stmt)
        return {str(label): str(name) for label, name in rows.all()}

    async def speakers_by_name(
        self, user_id: uuid.UUID, name: str,
    ) -> list[Speaker]:
        """People whose name contains `name`, case-insensitively.

        Substring rather than exact match: a caller filtering by person has a
        name a human typed or read off a transcript, not an id.
        """
        needle = (name or "").strip()
        if not needle:
            return []
        stmt = select(Speaker).where(
            Speaker.user_id == user_id, Speaker.name.ilike(f"%{needle}%")
        ).order_by(Speaker.name.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def diarized_task_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """Tasks with at least one label bound to a named person.

        "Diarised" here means IDENTIFIED, not merely split into speakers: a
        task whose labels were all left anonymous has no names to show and no
        person to filter by, so counting it would make `diarized=True` return
        rows indistinguishable from the ones it excludes.
        """
        stmt = (
            select(MatchDecision.source_task_id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.speaker_id.isnot(None),
                MatchDecision.source_task_id.isnot(None),
            )
            .distinct()
        )
        return [r[0] for r in (await self.session.execute(stmt)).all() if r[0]]

    async def speaker_names_for_tasks(
        self, user_id: uuid.UUID, task_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[str]]:
        """Named people per task, for a whole page of tasks in ONE query.

        The list-shaped sibling of `speaker_names_for_task`. Listing N tasks
        must not cost N queries, and a page of 50 would do exactly that if the
        single-task helper were called in a loop.

        Only NAMED people appear: the join to Speaker drops labels left
        anonymous or whose person was deleted. So an empty list means "nothing
        named here", which is also the honest answer for a task that was never
        diarised — the caller does not have to distinguish those to render.
        """
        if not task_ids:
            return {}
        stmt = (
            select(MatchDecision.source_task_id, Speaker.name)
            .join(Speaker, MatchDecision.speaker_id == Speaker.id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id.in_(task_ids),
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        out: dict[uuid.UUID, list[str]] = {}
        for task_id, name in (await self.session.execute(stmt)).all():
            names = out.setdefault(task_id, [])
            # De-duplicated: one person may hold several labels in one task,
            # and a reader wants the cast list, not the label count.
            if str(name) not in names:
                names.append(str(name))
        return out

    async def speaker_labels_for_tasks(
        self, user_id: uuid.UUID, task_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, dict[str, str]]:
        """Per task, the label -> name map, for a whole page in ONE query.

        The batched form of `speaker_names_for_task`. Pairing by LABEL and not
        by position matters: a chunk records which labels spoke in it, and
        guessing the name from their order would attach the wrong person to a
        quote whenever a passage does not start with speaker 00.
        """
        if not task_ids:
            return {}
        stmt = (
            select(MatchDecision.source_task_id, MatchDecision.speaker_label, Speaker.name)
            .join(Speaker, MatchDecision.speaker_id == Speaker.id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id.in_(task_ids),
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        out: dict[uuid.UUID, dict[str, str]] = {}
        # Ascending order means the LATEST decision for a label wins, matching
        # speaker_names_for_task.
        for task_id, label, name in (await self.session.execute(stmt)).all():
            out.setdefault(task_id, {})[str(label)] = str(name)
        return out

    async def tasks_featuring_speaker(
        self, user_id: uuid.UUID, speaker_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Tasks where this person was identified — the filter's backing set."""
        stmt = (
            select(MatchDecision.source_task_id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.speaker_id == speaker_id,
            )
            .distinct()
        )
        return [r[0] for r in (await self.session.execute(stmt)).all() if r[0]]

    async def decisions_for_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> dict[str, dict[str, Any]]:
        """Per-label LATEST decision for this task: bound speaker_id, that
        speaker's current name, and is_noise.

        Used to seed the reopened voice-resolution dialog with what the operator
        actually saved, not the stale auto-match in speaker_matches.json (bug #1,
        vts-552). LEFT join to Speaker so a decision that left the label anonymous
        (speaker_id NULL) or whose person was deleted still appears — the caller
        needs to distinguish "decided anonymous" from "no decision at all". Latest
        wins per label via ascending order (mirrors speaker_names_for_task).
        """
        stmt = (
            select(
                MatchDecision.speaker_label,
                MatchDecision.speaker_id,
                Speaker.name,
                MatchDecision.is_noise,
            )
            .outerjoin(Speaker, MatchDecision.speaker_id == Speaker.id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == task_id,
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        rows = await self.session.execute(stmt)
        latest: dict[str, dict[str, Any]] = {}
        for label, speaker_id, name, is_noise in rows.all():
            latest[str(label)] = {
                # A bound person whose row was deleted (speaker_id set but name
                # None via the LEFT join) reads as unbound — the person is gone.
                "speaker_id": str(speaker_id) if (speaker_id and name is not None) else None,
                "name": str(name) if name is not None else None,
                "is_noise": bool(is_noise),
            }
        return latest

    async def noise_labels_from_decisions(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> set[str]:
        """Labels whose LATEST decision for this task is is_noise=True.

        An empty result does NOT mean "no decisions" — it can equally mean
        "decisions exist and the operator marked nobody as noise". Callers that
        must distinguish those cases (to decide whether to honour an explicit
        all-clear over the auto-suggestion) use `has_decisions_for_task`
        separately (vts-552). Latest wins per label: ordered so the last decision
        in a re-save overrides earlier ones, mirroring speaker_names_for_task.
        """
        stmt = (
            select(MatchDecision.speaker_label, MatchDecision.is_noise)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == task_id,
            )
            .order_by(MatchDecision.created_at.asc(), MatchDecision.id.asc())
        )
        rows = await self.session.execute(stmt)
        latest: dict[str, bool] = {}
        for label, is_noise in rows.all():
            latest[str(label)] = bool(is_noise)
        return {label for label, is_noise in latest.items() if is_noise}

    async def has_decisions_for_task(
        self, user_id: uuid.UUID, task_id: uuid.UUID,
    ) -> bool:
        """Whether the operator has saved ANY resolution for this task.

        The noise resolver needs this to tell "no decisions yet" (auto mode ->
        trust speaker_matches.json) from "decisions exist, none are noise" (the
        operator's explicit all-clear -> honour it, ignore the auto-suggestion).
        `noise_labels_from_decisions` alone cannot distinguish them (vts-552).
        """
        stmt = (
            select(MatchDecision.id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == task_id,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def merge_speakers(
        self, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID,
    ) -> bool:
        """Merge source into target: samples + decisions move to target, source deleted.

        Order matters: rewrite decisions BEFORE deleting source, so the source
        delete's SET NULL finds no decisions pointing at it — names survive in old
        tasks. Merge asserts 'same person', so rewriting speaker_id does not distort
        the decisions' calibration (distance/outcome unchanged).
        """
        if source_id == target_id:
            return False
        source = await self.get_speaker(user_id, source_id)
        target = await self.get_speaker(user_id, target_id)
        if source is None or target is None:
            return False
        await self.reassign_speaker_samples(user_id, source_id, target_id)
        await self.session.execute(
            update(MatchDecision)
            .where(MatchDecision.user_id == user_id, MatchDecision.speaker_id == source_id)
            .values(speaker_id=target_id)
        )
        await self.session.delete(source)
        await self.session.flush()
        return True

    async def find_prior_decision_sample(
        self, user_id: uuid.UUID, source_task_id: uuid.UUID, speaker_label: str,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
        """Most recent decision this user recorded for (source_task_id, speaker_label).

        Returns (speaker_id, voice_sample_id) from that decision, or None if no
        prior decision exists — used to detect a rebind within the same
        awaiting_input dialog so the fragment it previously added can be rolled
        back. Ordered by created_at desc to pick the latest if resolved more
        than twice.
        """
        stmt = (
            select(MatchDecision.speaker_id, MatchDecision.voice_sample_id)
            .where(
                MatchDecision.user_id == user_id,
                MatchDecision.source_task_id == source_task_id,
                MatchDecision.speaker_label == speaker_label,
            )
            .order_by(MatchDecision.created_at.desc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return (row[0], row[1])

    async def record_decision(
        self, *, user_id: uuid.UUID, source_task_id: uuid.UUID | None, speaker_label: str,
        speaker_id: uuid.UUID | None, voice_sample_id: uuid.UUID | None,
        distance: float | None, embedding_model: str, outcome: str,
        is_noise: bool = False,
    ) -> MatchDecision:
        row = MatchDecision(
            user_id=user_id, source_task_id=source_task_id, speaker_label=speaker_label,
            speaker_id=speaker_id, voice_sample_id=voice_sample_id, distance=distance,
            embedding_model=embedding_model, outcome=outcome, is_noise=is_noise,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def nearest_speakers(
        self, user_id: uuid.UUID, embedding: list[float], embedding_model: str,
        limit: int | None = None,
    ) -> list[tuple[Speaker, float]]:
        """User's speakers ranked by their nearest fragment (MIN cosine distance).

        Only samples computed by `embedding_model` count: distances across models
        are meaningless. `<=>` is cosine — smaller is nearer.
        """
        dist = func.min(VoiceSample.embedding.cosine_distance(embedding)).label("dist")
        stmt = (
            select(Speaker, dist)
            .join(VoiceSample, VoiceSample.speaker_id == Speaker.id)
            .where(Speaker.user_id == user_id, VoiceSample.embedding_model == embedding_model)
            .group_by(Speaker.id)
            .order_by(dist.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self.session.execute(stmt)
        return [(row[0], float(row[1])) for row in rows.all()]

    async def load_sample_audio(self, user_id: uuid.UUID, sample_id: uuid.UUID) -> tuple[bytes, str] | None:
        stmt = (
            select(VoiceSample)
            .join(Speaker, VoiceSample.speaker_id == Speaker.id)
            .where(VoiceSample.id == sample_id, Speaker.user_id == user_id)
            .options(undefer(VoiceSample.audio))
        )
        sample = await self.session.scalar(stmt)
        if sample is None:
            return None
        return sample.audio, sample.audio_format

    # ------------------------------------------------------------------
    # Per-user step weights (vts-8cm)
    # ------------------------------------------------------------------

    async def step_durations_for_user(self, user_id: uuid.UUID) -> list[StepDuration]:
        stmt = (
            select(Step.name, Step.started_at, Step.finished_at, Task.summary_progress)
            .join(Task, Step.task_id == Task.id)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.completed,
                Step.status == StepStatus.completed,
                Step.started_at.is_not(None),
                Step.finished_at.is_not(None),
            )
        )
        rows: list[StepDuration] = []
        for name, started, finished, summary_progress in await self.session.execute(stmt):
            duration = (finished - started).total_seconds()
            if duration < 0:
                continue
            total = None
            if isinstance(summary_progress, dict):
                raw_total = summary_progress.get("total")
                if isinstance(raw_total, int) and raw_total >= 1:
                    total = raw_total
            rows.append(StepDuration(name, duration, total))
        return rows

    async def upsert_user_step_weights(
        self,
        user_id: uuid.UUID,
        weights: dict,
        final_summary_fallback: float | None,
        computed_at: datetime,
        sample_counts: dict,
    ) -> UserStepWeights:
        row = await self.get_user_step_weights(user_id)
        if row is None:
            row = UserStepWeights(user_id=user_id)
            self.session.add(row)
        row.weights = weights
        row.final_summary_fallback = final_summary_fallback
        row.computed_at = computed_at
        row.sample_counts = sample_counts
        await self.session.flush()
        return row

    async def get_user_step_weights(self, user_id: uuid.UUID) -> UserStepWeights | None:
        return await self.session.scalar(
            select(UserStepWeights).where(UserStepWeights.user_id == user_id)
        )

    async def users_with_completed_tasks(self) -> list[uuid.UUID]:
        stmt = (
            select(Task.user_id)
            .where(Task.status == TaskStatus.completed)
            .distinct()
        )
        return list(await self.session.scalars(stmt))

    # ------------------------------------------------------------------
    # Tags (vts-qwcj)
    # ------------------------------------------------------------------

    async def list_tags(self, user_id: uuid.UUID) -> list[tuple[Tag, int]]:
        """Every tag of the user with how many recordings carry it.

        Counts all linked recordings, listed or not: a tag's count is about the
        tag, and an unlisted recording still holds the link.
        """
        stmt = (
            select(Tag, func.count(RecordingTag.recording_id))
            .outerjoin(RecordingTag, RecordingTag.tag_id == Tag.id)
            .where(Tag.user_id == user_id)
            .group_by(Tag.id)
            .order_by(Tag.name_key)
        )
        return [(tag, int(n)) for tag, n in (await self.session.execute(stmt)).all()]

    async def create_tag(self, user_id: uuid.UUID, name: str) -> Tag:
        name = normalize_tag_name(name)
        key = name.casefold()
        if await self._tag_by_key(user_id, key) is not None:
            raise TagExists(name)
        tag = Tag(user_id=user_id, name=name, name_key=key)
        async with self._unique_tag_write(name):
            self.session.add(tag)
        return tag

    async def rename_tag(self, user_id: uuid.UUID, tag_id: uuid.UUID, name: str) -> Tag | None:
        tag = await self.session.scalar(
            select(Tag).where(Tag.id == tag_id, Tag.user_id == user_id)
        )
        if tag is None:
            return None
        name = normalize_tag_name(name)
        key = name.casefold()
        if key != tag.name_key and await self._tag_by_key(user_id, key) is not None:
            raise TagExists(name)
        async with self._unique_tag_write(name):
            tag.name = name
            tag.name_key = key
        return tag

    async def delete_tag(self, user_id: uuid.UUID, tag_id: uuid.UUID) -> int | None:
        """Delete the tag; returns how many recordings it was unlinked from."""
        tag = await self.session.scalar(
            select(Tag).where(Tag.id == tag_id, Tag.user_id == user_id)
        )
        if tag is None:
            return None
        unlinked = await self.session.scalar(
            select(func.count()).select_from(RecordingTag).where(RecordingTag.tag_id == tag_id)
        )
        await self.session.execute(delete(Tag).where(Tag.id == tag_id))
        await self.session.flush()
        return int(unlinked or 0)

    async def resolve_tags(
        self, user_id: uuid.UUID, names: list[str], *, create: bool
    ) -> list[Tag]:
        """The user's tags for these names (by key), in the order given.

        Unknown names are created when `create`, otherwise skipped.
        """
        wanted = dedupe_names(names)
        if not wanted:
            return []
        keys = [n.casefold() for n in wanted]
        if create:
            # DO NOTHING rather than check-then-insert: two requests creating
            # the same tag race, and one of them must not fail on the unique key.
            await self.session.execute(
                pg_insert(Tag)
                .values([
                    {"id": uuid.uuid4(), "user_id": user_id, "name": n,
                     "name_key": n.casefold(), "created_at": datetime.now(timezone.utc)}
                    for n in wanted
                ])
                .on_conflict_do_nothing(index_elements=["user_id", "name_key"])
            )
        rows = await self.session.scalars(
            select(Tag).where(Tag.user_id == user_id, Tag.name_key.in_(keys))
        )
        by_key = {t.name_key: t for t in rows}
        return [by_key[k] for k in keys if k in by_key]

    async def set_recording_tags(self, recording: Recording, names: list[str]) -> list[Tag]:
        """Replace the recording's tag set; names that do not exist are created."""
        tags = await self.resolve_tags(recording.user_id, names, create=True)
        wanted = {t.id for t in tags}
        await self.session.execute(
            delete(RecordingTag).where(
                RecordingTag.recording_id == recording.id,
                RecordingTag.tag_id.not_in(wanted) if wanted else sa_true(),
            )
        )
        await self._link(recording.id, wanted)
        return sorted(tags, key=lambda t: t.name_key)

    async def change_recording_tags(
        self, recording: Recording, add: list[str], remove: list[str]
    ) -> list[Tag]:
        """Add and remove tags without touching the others.

        `add` creates missing tags; `remove` matches by key and ignores names
        the user has no tag for.
        """
        added = await self.resolve_tags(recording.user_id, add, create=True)
        await self._link(recording.id, {t.id for t in added})
        removed = await self.resolve_tags(recording.user_id, remove, create=False)
        removed_ids = {t.id for t in removed} - {t.id for t in added}
        if removed_ids:
            await self.session.execute(
                delete(RecordingTag).where(
                    RecordingTag.recording_id == recording.id,
                    RecordingTag.tag_id.in_(removed_ids),
                )
            )
        return (await self.tags_for_recordings([recording.id])).get(recording.id, [])

    async def own_tag_ids(self, user_id: uuid.UUID, ids: Iterable[str]) -> set[str]:
        """Which of these ids (strings, as presets store them) are the user's
        existing tags. Malformed ids are simply not among them."""
        parsed: set[uuid.UUID] = set()
        for raw in ids:
            try:
                parsed.add(uuid.UUID(str(raw)))
            except ValueError:
                continue
        if not parsed:
            return set()
        rows = await self.session.scalars(
            select(Tag.id).where(Tag.user_id == user_id, Tag.id.in_(parsed))
        )
        return {str(i) for i in rows}

    async def tags_by_ids(self, user_id: uuid.UUID, ids: Iterable[str]) -> list[Tag]:
        """The user's tags among these ids (strings, as presets store them),
        in the order given; unknown, foreign and malformed ids are skipped."""
        own = await self.own_tag_ids(user_id, ids)
        if not own:
            return []
        rows = await self.session.scalars(
            select(Tag).where(Tag.user_id == user_id, Tag.id.in_([uuid.UUID(i) for i in own]))
        )
        by_id = {str(t.id): t for t in rows}
        return [by_id[i] for i in dict.fromkeys(str(x) for x in ids) if i in by_id]

    async def tags_for_recordings(
        self, recording_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[Tag]]:
        """One query; recordings without tags are absent from the result."""
        if not recording_ids:
            return {}
        stmt = (
            select(RecordingTag.recording_id, Tag)
            .join(Tag, Tag.id == RecordingTag.tag_id)
            .where(RecordingTag.recording_id.in_(recording_ids))
            .order_by(Tag.name_key)
        )
        out: dict[uuid.UUID, list[Tag]] = {}
        for rid, tag in (await self.session.execute(stmt)).all():
            out.setdefault(rid, []).append(tag)
        return out

    @asynccontextmanager
    async def _unique_tag_write(self, name: str):
        """Wrap a tag insert/rename. The savepoint is opened BEFORE the write is
        staged (begin_nested flushes pending state, which must not happen in the
        outer transaction). A unique-key violation, i.e. a concurrent writer won
        the race past our check, becomes TagExists and only the savepoint is
        rolled back, so the outer transaction stays usable."""
        try:
            async with self.session.begin_nested():
                yield
                await self.session.flush()
        except IntegrityError as exc:
            raise TagExists(name) from exc

    async def tag_by_name(self, user_id: uuid.UUID, name: str) -> Tag | None:
        """The user's tag a name resolves to (trimmed, case-insensitive)."""
        return await self._tag_by_key(user_id, normalize_tag_name(name).casefold())

    async def _tag_by_key(self, user_id: uuid.UUID, key: str) -> Tag | None:
        return await self.session.scalar(
            select(Tag).where(Tag.user_id == user_id, Tag.name_key == key)
        )

    async def _link(self, recording_id: uuid.UUID, tag_ids: set[uuid.UUID]) -> None:
        if not tag_ids:
            return
        await self.session.execute(
            pg_insert(RecordingTag)
            .values([{"recording_id": recording_id, "tag_id": t} for t in tag_ids])
            .on_conflict_do_nothing()
        )
