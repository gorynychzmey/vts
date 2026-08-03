"""A stray `alembic upgrade head` must not migrate the production database.

vts-66i, measured: run from a checkout on the prod host, `alembic upgrade head`
applied migrations to the LIVE database. vts.core.config.Settings reads
/opt/vts/config/config.yaml, that file points at the prod Postgres, and nothing
in the command hints at the target — so the command looks local and harmless.
The result was a real split: prod DB at 0021_delivery_attempts while the
running containers only knew 0019, and `alembic current` inside the container
failed with "Can't locate revision 0021_delivery_attempts".

The guard belongs here rather than in vts.db.preflight, because preflight only
runs from the container entrypoint — a bare `alembic` from a checkout skips it,
which is precisely the path that fired.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vts.db.migration_guard import (
    ProdMigrationRefused,
    ensure_migration_allowed,
)


def test_prod_is_refused_without_the_opt_in():
    """The whole point: prod + no override = refuse, before any DDL runs."""
    with pytest.raises(ProdMigrationRefused) as excinfo:
        ensure_migration_allowed(
            environment="prod",
            database_url="postgresql+asyncpg://vts:secret@beelink.fritz.box:5432/vts",
            allow_flag=None,
        )

    message = str(excinfo.value)
    # The operator must be able to act on it: which database, and how to proceed.
    assert "beelink.fritz.box" in message, message
    assert "VTS_ALLOW_PROD_MIGRATIONS=1" in message, message
    # Never echo the password back into a log.
    assert "secret" not in message, message


def test_prod_is_allowed_with_the_explicit_opt_in():
    """Deploys must still work — the container sets the flag deliberately."""
    ensure_migration_allowed(
        environment="prod",
        database_url="postgresql+asyncpg://vts:secret@beelink.fritz.box:5432/vts",
        allow_flag="1",
    )


def test_dev_needs_no_opt_in():
    """Local work against a dev database stays frictionless."""
    ensure_migration_allowed(
        environment="dev",
        database_url="postgresql+asyncpg://vts:vts@localhost:5432/vts_test",
        allow_flag=None,
    )


@pytest.mark.parametrize("flag", ["0", "", "false", "no"])
def test_only_a_real_opt_in_counts(flag):
    """A present-but-falsey flag must not be mistaken for consent.

    `VTS_ALLOW_PROD_MIGRATIONS=0` reads as "definitely not"; treating any
    non-empty value as opt-in would invert that.
    """
    with pytest.raises(ProdMigrationRefused):
        ensure_migration_allowed(
            environment="prod",
            database_url="postgresql+asyncpg://vts@db/vts",
            allow_flag=flag,
        )


def test_refusal_message_names_the_environment_source():
    """The message has to explain WHY it thinks this is prod, or the reader
    cannot tell whether the guard is right."""
    with pytest.raises(ProdMigrationRefused) as excinfo:
        ensure_migration_allowed(
            environment="prod",
            database_url="postgresql+asyncpg://vts@db/vts",
            allow_flag=None,
        )
    assert "config.yaml" in str(excinfo.value), str(excinfo.value)


def test_unreadable_prod_config_falls_back_to_the_local_one(tmp_path, monkeypatch):
    """An unreadable /opt/vts/config/config.yaml must not crash arbitrary commands.

    Once vts-bz6 tightened that file to 600 root:root, every non-root process on
    the prod host — a checkout, a script, `alembic` — died on a raw
    `PermissionError: [Errno 13]` from _load_yaml_overrides, because the loader
    opened the path unconditionally. Not being allowed to read the production
    config is the normal, healthy case for an unprivileged user; it should mean
    "these overrides are not for you", not "crash".
    """
    # conftest replaces vts.core.config._load_yaml_overrides with `lambda: {}`
    # at import time (before collection, deliberately), so the module attribute
    # is the stub by the time any test runs. Load the module source under its
    # own name to get the genuine function without disturbing the shared
    # module object that the rest of the suite — and the fixture — relies on.
    import importlib.util

    import vts.core.config

    spec = importlib.util.spec_from_file_location(
        "_vts_config_under_test", Path(vts.core.config.__file__)
    )
    pristine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pristine)

    unreadable = tmp_path / "prod-config.yaml"
    unreadable.write_text("environment:\n  productive: true\n", encoding="utf-8")
    unreadable.chmod(0o000)

    local = tmp_path / "config.yaml"
    local.write_text("environment:\n  productive: false\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pristine, "_DEFAULT_CONFIG_PATH", unreadable)

    # Must not raise, and must fall through to the local file rather than
    # silently inheriting prod's settings.
    overrides = pristine._load_yaml_overrides()
    assert overrides.get("environment") == "dev", overrides
