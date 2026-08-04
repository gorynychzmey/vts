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
#: 1.1 added connection_fields() (vts-929). Made a REQUIRED Protocol member
#: rather than an optional getattr, by explicit decision: no plugins exist
#: yet — in-tree vts-outline is the only adapter — so nothing can be broken
#: by requiring it, and an optional member would leave the core guessing at
#: the connection/parameter split forever. Once third-party plugins exist,
#: the add-only rule above applies again in full.
CONTRACT_VERSION = (1, 1)


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

    def connection_fields(self) -> list[str]:
        """Names of the fields (config AND secret) that identify a CONNECTION.

        Everything else this adapter declares is a per-destination parameter.
        The core stores connection fields once, in a shared credential, and
        keeps parameters on each target, so two destinations on the same
        server do not duplicate the endpoint or its token (vts-929).

        The split is declared HERE, by the adapter, and never inferred by the
        core: "a credential is a URL plus a token" happens to fit Outline, but
        delivery to a local folder has no connection at all, S3 splits as
        keys+region+bucket, and email as SMTP host+login vs recipients.
        Hard-coding any one of those shapes into the core would freeze it
        around today's single plugin.

        Return an empty list if this adapter needs no connection; the core
        then requires no credential for its targets.
        """
        ...
    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult: ...
