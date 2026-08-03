"""Refuse to migrate the production database unless it was asked for explicitly.

vts-66i: `alembic upgrade head`, run from a checkout on the prod host, migrated
the LIVE database. Nothing about the command said so — `vts.core.config`
reads /opt/vts/config/config.yaml, which points at the prod Postgres, so a
command that reads as "migrate my local dev DB" silently reached prod. It left
the schema ahead of the running image (prod at 0021, containers knowing only
0019) and `alembic current` failing inside the container.

The check lives here, and is called from `alembic/env.py`, because that is the
one path every migration takes. `vts.db.preflight` runs only from the container
entrypoint, so a bare `alembic` from a checkout bypasses it — which is exactly
the case that fired.

Deliberate deploys stay unblocked: the pod's migrate initContainer sets
VTS_ALLOW_PROD_MIGRATIONS=1, which is a visible, greppable statement of intent
rather than an accident.
"""
from __future__ import annotations

ALLOW_ENV_VAR = "VTS_ALLOW_PROD_MIGRATIONS"

# Values that mean "yes". Anything else — including "0", "false" and "" — is
# treated as no: `VTS_ALLOW_PROD_MIGRATIONS=0` must not read as consent.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ProdMigrationRefused(RuntimeError):
    """A migration targeted production without the explicit opt-in."""


def ensure_migration_allowed(
    environment: str,
    database_url: str,
    allow_flag: str | None,
) -> None:
    """Raise ProdMigrationRefused if this would migrate prod unasked.

    `environment` comes from Settings, where `environment.productive: true` in
    config.yaml maps to "prod". `allow_flag` is the raw VTS_ALLOW_PROD_MIGRATIONS
    value (None when unset), passed in rather than read here so the decision is
    testable without touching the process environment.
    """
    if environment.strip().lower() != "prod":
        return
    if allow_flag is not None and allow_flag.strip().lower() in _TRUTHY:
        return

    raise ProdMigrationRefused(
        f"Refusing to run migrations: this targets the PRODUCTION database\n"
        f"  {safe_url(database_url)}\n"
        f"because config.yaml sets environment.productive: true (Settings"
        f".environment == 'prod').\n"
        f"\n"
        f"If you meant a local database, note that VTS_DATABASE_URL alone will\n"
        f"NOT redirect this: Settings is built as Settings(**yaml_overrides), so\n"
        f"config.yaml wins over the environment. The 'prod' verdict comes from a\n"
        f"config.yaml with environment.productive: true — either /opt/vts/config/\n"
        f"config.yaml on the deploy host, or the one in your working directory.\n"
        f"Run from a directory whose config.yaml sets productive: false.\n"
        f"\n"
        f"If you really do mean production, say so explicitly:\n"
        f"  {ALLOW_ENV_VAR}=1 alembic upgrade head\n"
        f"(the deploy's migrate step sets this; see docker/vts-entrypoint.sh)."
    )


def safe_url(url: str) -> str:
    """Render a DSN without its password, for messages and logs."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - a bad URL must not mask the refusal
        return "<unparseable database url>"
