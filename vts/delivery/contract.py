from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class DeliveryError(Exception):
    """Raised by an adapter to signal a retryable delivery failure."""


@dataclass(frozen=True)
class TaskMeta:
    source_url: str
    source_title: str | None
    language: str | None
    duration_s: float | None
    created_at: datetime


@dataclass(frozen=True)
class DeliveryPayload:
    task_id: str
    variant: str
    content: str
    content_format: str
    task: TaskMeta


@dataclass(frozen=True)
class DeliveryTargetConfig:
    config: dict[str, Any]
    secrets: dict[str, str]


@dataclass(frozen=True)
class DeliveryResult:
    external_id: str | None = None
    external_url: str | None = None


@runtime_checkable
class DeliveryAdapter(Protocol):
    name: str

    def config_schema(self) -> dict: ...
    def secret_keys(self) -> list[str]: ...
    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult: ...
