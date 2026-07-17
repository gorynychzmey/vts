from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from vts.db.models import (
    ApiToken,
    AsrSegment,
    MatchDecision,
    Preset,
    Prompt,
    Speaker,
    Step,
    StepStatus,
    Task,
    TaskStatus,
    User,
    UserStepWeights,
    VoiceSample,
)
from vts.metrics.step_weights import StepDuration
from vts.services import task_status


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Repo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_user(self, username: str) -> User:
        stmt = select(User).where(User.username == username)
        user = await self.session.scalar(stmt)
        if user:
            return user
        user = User(username=username)
        self.session.add(user)
        await self.session.flush()
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
        await self.session.flush()

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

    async def get_task_for_user(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Task | None:
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id, Task.id == task_id)
        )
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
        task.updated_at = utcnow()
        await self.session.flush()

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
        )
        self.session.add(segment)
        await self.session.flush()
        return segment

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
        await self.session.flush()
        return segment

    async def get_task_segments(self, task_id: uuid.UUID) -> list[AsrSegment]:
        stmt = select(AsrSegment).where(AsrSegment.task_id == task_id).order_by(AsrSegment.segment_index.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_asr_progress_for_tasks(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        if not task_ids:
            return {}
        stmt = select(AsrSegment.task_id, AsrSegment.raw_json).where(AsrSegment.task_id.in_(task_ids))
        result = await self.session.execute(stmt)
        progress: dict[uuid.UUID, tuple[int, int]] = {}
        for task_id, raw_json in result.all():
            done, total = progress.get(task_id, (0, 0))
            total += 1
            if isinstance(raw_json, dict) and bool(raw_json):
                done += 1
            progress[task_id] = (done, total)
        return progress

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
            .limit(1)
        )
        candidate = await self.session.scalar(stmt)
        if candidate is None:
            return None
        # Compare options exactly
        if candidate.options != options:
            return None
        return candidate

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

    async def create_prompt(self, user_id: uuid.UUID, name: str, system_prompt: str) -> Prompt:
        prompt = Prompt(user_id=user_id, name=name, system_prompt=system_prompt)
        self.session.add(prompt)
        await self.session.flush()
        return prompt

    async def list_prompts(self, user_id: uuid.UUID) -> list[Prompt]:
        stmt = (
            select(Prompt)
            .where(Prompt.user_id == user_id)
            .order_by(Prompt.created_at.desc())
        )
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
        new_options = dict(task.options or {})
        new_options["prompt_results"] = prompt_results
        task.options = new_options  # reassign so SQLAlchemy flushes the JSON column
        task.updated_at = utcnow()
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

    async def list_voice_samples(self, speaker_id: uuid.UUID) -> list[VoiceSample]:
        # audio stays deferred — never loaded here
        stmt = (
            select(VoiceSample)
            .where(VoiceSample.speaker_id == speaker_id)
            .order_by(VoiceSample.created_at.asc())
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
    ) -> MatchDecision:
        row = MatchDecision(
            user_id=user_id, source_task_id=source_task_id, speaker_label=speaker_label,
            speaker_id=speaker_id, voice_sample_id=voice_sample_id, distance=distance,
            embedding_model=embedding_model, outcome=outcome,
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
