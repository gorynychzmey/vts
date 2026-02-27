from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vts.db.models import AsrSegment, AsrWord, Step, StepStatus, Task, TaskStatus, User


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
    ) -> Task:
        task = Task(
            id=task_id or uuid.uuid4(),
            user_id=user_id,
            source_url=source_url,
            options=options,
            artifact_dir=artifact_dir,
            status=TaskStatus.queued,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def list_tasks_for_user(self, user_id: uuid.UUID) -> list[Task]:
        stmt = (
            select(Task)
            .options(selectinload(Task.steps))
            .where(Task.user_id == user_id)
            .order_by(Task.created_at.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_task_ids_for_statuses(self, statuses: list[TaskStatus]) -> list[uuid.UUID]:
        stmt = select(Task.id).where(Task.status.in_(statuses)).order_by(Task.created_at.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def requeue_running_tasks(self) -> list[uuid.UUID]:
        stmt = select(Task).where(Task.status == TaskStatus.running)
        result = await self.session.scalars(stmt)
        tasks = list(result.all())
        for task in tasks:
            task.status = TaskStatus.queued
            task.error_message = None
            task.updated_at = utcnow()
        await self.session.flush()
        return [task.id for task in tasks]

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

    async def upsert_step(self, task_id: uuid.UUID, name: str) -> Step:
        stmt = select(Step).where(Step.task_id == task_id, Step.name == name)
        step = await self.session.scalar(stmt)
        if step:
            return step
        step = Step(task_id=task_id, name=name, status=StepStatus.pending)
        self.session.add(step)
        await self.session.flush()
        return step

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

    async def add_asr_words(
        self, task_id: uuid.UUID, segment_id: uuid.UUID, words: list[dict[str, object]]
    ) -> None:
        for word in words:
            record = AsrWord(
                task_id=task_id,
                segment_id=segment_id,
                word=str(word.get("word", "")).strip(),
                start_sec=float(word.get("start", 0.0)),
                end_sec=float(word.get("end", 0.0)),
                confidence=float(word["confidence"]) if word.get("confidence") is not None else None,
            )
            self.session.add(record)
        await self.session.flush()

    async def replace_asr_words(
        self, task_id: uuid.UUID, segment_id: uuid.UUID, words: list[dict[str, object]]
    ) -> None:
        await self.session.execute(delete(AsrWord).where(AsrWord.segment_id == segment_id))
        await self.add_asr_words(task_id=task_id, segment_id=segment_id, words=words)

    async def get_task_segments(self, task_id: uuid.UUID) -> list[AsrSegment]:
        stmt = select(AsrSegment).where(AsrSegment.task_id == task_id).order_by(AsrSegment.segment_index.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_task_words(self, task_id: uuid.UUID) -> list[AsrWord]:
        stmt = select(AsrWord).where(AsrWord.task_id == task_id).order_by(AsrWord.start_sec.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def get_words_for_segment(self, segment_id: uuid.UUID) -> list[AsrWord]:
        stmt = select(AsrWord).where(AsrWord.segment_id == segment_id).order_by(AsrWord.start_sec.asc())
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def clear_asr_for_task(self, task_id: uuid.UUID) -> None:
        await self.session.execute(delete(AsrWord).where(AsrWord.task_id == task_id))
        await self.session.execute(delete(AsrSegment).where(AsrSegment.task_id == task_id))
        await self.session.flush()
