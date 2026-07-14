from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


@dataclass
class StepState:
    task_id: uuid.UUID
    user_id: str
    dirs: dict[str, Path]
    logger: logging.Logger
    task_options: dict[str, Any]


class Step(ABC):
    name: str
    lane: str | None = None

    @abstractmethod
    async def run(self, ctx: "PipelineContext", st: StepState) -> None: ...

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return False
