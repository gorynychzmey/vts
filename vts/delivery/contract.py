from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


#: Version of the adapter contract this core implements, as (major, minor).
#:
#: Evolution rule (project decision, vts-9y7) — applies to EVERY future change
#: to this module:
#:   - removing/renaming a field or method, or changing semantics or a
#:     signature -> bump MAJOR. Breaking: old adapters stop loading, on
#:     purpose and with a stated reason.
#:   - adding a new (optional) field or method -> bump MINOR. Backwards
#:     compatible: old adapters keep loading.
#: Invariant: within one major, additions only. Otherwise the min-compatible
#: check in the registry becomes a lie.
#: On a MAJOR bump, plugin CODE must be reviewed, not merely rebuilt — a
#: breaking change can affect adapter logic, not just signatures.
CONTRACT_VERSION = (1, 0)


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

    #: Minimum core contract this adapter needs, as (major, minor). The
    #: registry loads the adapter iff plugin.major == core.major and
    #: plugin.minor <= core.minor (see registry.CONTRACT_VERSION).
    #:
    #: NOTE: isinstance() against a runtime_checkable Protocol only checks
    #: that attributes EXIST — never their types. An adapter declaring
    #: contract_version = "1.0" passes isinstance and would then blow up on
    #: indexing, so the registry validates the shape of this value explicitly
    #: rather than trusting the Protocol check.
    contract_version: tuple[int, int]

    def config_schema(self) -> dict: ...
    def secret_keys(self) -> list[str]: ...
    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult: ...
