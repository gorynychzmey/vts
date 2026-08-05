from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
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
#: yet — vts-outline was the only adapter — so nothing can be broken
#: by requiring it, and an optional member would leave the core guessing at
#: the connection/parameter split forever. Once third-party plugins exist,
#: the add-only rule above applies again in full.
#: 1.2 added check_connection() and config_options() (vts-6o37). Both are
#: OPTIONAL, read via getattr rather than declared on the Protocol — the
#: opposite of the 1.1 decision above, and deliberately so: vts-outline is now
#: published and installed from a GitHub release, so "no plugins exist yet"
#: no longer holds and the add-only rule binds in full. An adapter without
#: them must keep loading; delivery to a local folder has neither a connection
#: to test nor an external directory to list.
#: 1.3 published the timing budget for interactive adapter calls (vts-6o37
#: followup). Adding module constants is backwards compatible, so old adapters
#: keep loading — but an adapter that IMPORTS them requires 1.3 and must say
#: so, since the name does not exist on an older core.
CONTRACT_VERSION = (1, 3)


#: How long an adapter may spend inside one interactive call
#: (`check_connection`, `config_options`) before the core stops waiting.
#:
#: Published because it is part of the protocol, not an implementation detail:
#: an adapter's own HTTP timeout has to fit inside it. Before this, the number
#: lived as a literal in the core and every plugin had to guess or copy it —
#: the same defect as the hard-coded variant enum (vts-6fya), where a plugin
#: duplicated the core's knowledge and went stale the moment the core moved.
#:
#: An overrun is not a hang: the core cancels the call. But it IS a silent
#: degradation — everything the adapter was about to say about the cause is
#: lost, and the user sees a generic failure instead of "timed out".
INTERACTIVE_CALL_LIMIT_S = 20.0

#: What an adapter should actually budget for itself. Deliberately lower than
#: the limit above, so a plugin that finishes just inside its own deadline
#: still gets its answer back before the core gives up.
#:
#: Both are published on purpose: an adapter sets its client timeout from
#: this one, while the limit explains WHY exceeding it kills the call —
#: publishing only the budget would leave that unexplained. Adapters should
#: use this value rather than deriving their own fraction of the limit, which
#: is a decision every plugin would make differently and some would get wrong.
ADAPTER_CALL_BUDGET_S = 15.0


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


class CheckOutcome(str, Enum):
    """Why a connection check succeeded or failed.

    A code rather than a message: the adapter knows WHAT went wrong, the core
    knows how to say it in the user's language. Leaving adapters to phrase
    their own errors would scatter i18n across every plugin and give the same
    failure a different wording in each.
    """

    ok = "ok"
    #: The host does not resolve, or nothing is listening.
    unreachable = "unreachable"
    #: Reached it, but the credentials were refused.
    unauthorized = "unauthorized"
    #: Reached and authenticated, but the target of the config is not there
    #: (a missing collection, bucket, folder).
    not_found = "not_found"
    #: Answered, but not in a way this adapter understands — often a URL
    #: pointing at something that is not the expected API.
    unexpected_response = "unexpected_response"
    #: Took too long.
    timeout = "timeout"
    #: Anything the adapter cannot classify. `detail` carries what it knows.
    error = "error"


@dataclass(frozen=True)
class CheckResult:
    """What `check_connection` reports back.

    `detail` is optional free text (a status line, an exception message) shown
    beneath the localised message — useful when the code alone is too coarse
    to act on. It is NOT localised and must never carry a secret.
    """

    outcome: CheckOutcome
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is CheckOutcome.ok


@dataclass(frozen=True)
class ConfigOption:
    """One choice for a config field the adapter can enumerate.

    `value` is stored, `label` is shown. They differ on purpose: an Outline
    collection is addressed by a stable id but recognised by its name, and a
    rename must not break a target.
    """

    value: str
    label: str


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

    # --- optional, contract 1.2 (vts-6o37) --------------------------------
    #
    # These are NOT declared as Protocol members on purpose. A runtime
    # Protocol check requires every member to exist, so declaring them would
    # stop every already-published adapter from loading — exactly the
    # backwards-incompatible break the add-only rule forbids. The core reads
    # them with getattr and simply offers less when they are absent:
    #
    #   async def check_connection(
    #       self, target: DeliveryTargetConfig
    #   ) -> CheckResult:
    #       """Prove the endpoint is reachable and the credentials work.
    #
    #       Cheap and read-only — it runs from a web request while the user
    #       waits. Return a CheckResult; do not raise for an expected failure,
    #       since the outcome code is what the UI turns into a message."""
    #
    #   async def config_options(
    #       self, field: str, target: DeliveryTargetConfig
    #   ) -> list[ConfigOption]:
    #       """Enumerate the valid values of one config field.
    #
    #       For a field whose values live in the external system (an Outline
    #       collection, an S3 bucket). Raise if the system cannot be reached —
    #       the core degrades the field to free text rather than blocking the
    #       form."""
    #
    # An adapter that implements config_options should also list the fields it
    # can enumerate, so the core need not probe blindly:
    #
    #   def option_fields(self) -> list[str]: ...

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
