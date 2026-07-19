"""Database preflight checks that run before `alembic upgrade head`.

Migrations connect as the application role, which is deliberately not a
superuser. Anything needing superuser rights therefore cannot be done by a
migration and must be provisioned out of band (see
`scripts/setup_postgres.sh` and docs/INITIAL_DEPLOYMENT.md).

Without a preflight, that shows up the worst possible way: migration 0014
raises InsufficientPrivilegeError, startup aborts, systemd restarts the
unit, and the operator sees a crash loop and a 502 from the reverse proxy
with a raw driver traceback in the journal. Checking first turns it into a
single line naming the extension and the command that fixes it.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# Extensions the schema depends on. `vector` backs the Vector columns of the
# speaker registry (voice_samples.embedding), added in 1.4 / migration 0014.
REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector",)


class PreflightError(RuntimeError):
    """A required database precondition is not met.

    Raised instead of letting a migration fail on a privilege error, so the
    operator gets a message they can act on.
    """


async def missing_extensions(
    engine: AsyncEngine, required: Sequence[str] = REQUIRED_EXTENSIONS
) -> list[str]:
    """Return the subset of `required` that is not installed in the database."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
            {"names": list(required)},
        )
        installed = {row[0] for row in rows}
    return [name for name in required if name not in installed]


async def check_required_extensions(
    engine: AsyncEngine, required: Sequence[str] = REQUIRED_EXTENSIONS
) -> None:
    """Verify required extensions exist, or raise PreflightError explaining how
    to install them.

    Deliberately does not try to create anything: the application role has no
    privilege to do so, and attempting it is what produced the opaque failure
    this check exists to replace.
    """
    missing = await missing_extensions(engine, required)
    if not missing:
        return

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT current_user, current_database()")
        )
        role, database = result.one()

    statements = "\n".join(
        f"  psql -d {database} -c 'CREATE EXTENSION IF NOT EXISTS {name}'"
        for name in missing
    )
    raise PreflightError(
        f"Required Postgres extension(s) not installed: {', '.join(missing)}.\n"
        f"Migrations run as role '{role}', which is not a superuser and cannot "
        f"create them.\n"
        f"Install them once as a superuser against database '{database}':\n"
        f"{statements}\n"
        f"See docs/INITIAL_DEPLOYMENT.md (section 8, upgrade notes) for details."
    )


async def run_preflight(database_url: str) -> None:
    """Run all preflight checks against `database_url`.

    Called from the container entrypoint before migrations.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, echo=False)
    try:
        await check_required_extensions(engine)
    finally:
        await engine.dispose()
    logger.info("Database preflight passed: %s", ", ".join(REQUIRED_EXTENSIONS))


def main() -> int:
    """Entrypoint hook: `python -m vts.db.preflight`.

    Exits non-zero with the actionable message on failure so the container
    entrypoint stops before `alembic upgrade head` produces a traceback.
    """
    import asyncio

    from vts.core.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = get_settings().database_url
    # Log the target (credentials stripped) so a preflight that passes against
    # an unexpected database is visible rather than silently reassuring.
    logger.info("Running database preflight against %s", _safe_url(database_url))
    try:
        asyncio.run(run_preflight(database_url))
    except PreflightError as exc:
        logger.error("Database preflight failed.\n%s", exc)
        return 1
    return 0


def _safe_url(url: str) -> str:
    """Render a DSN without its password, for logging."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - never let logging break startup
        return "<unparseable database url>"


if __name__ == "__main__":
    raise SystemExit(main())
