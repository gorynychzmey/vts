"""The account-merge script must run the way its own docstring says to run it.

`scripts/merge_user_accounts.py` is a production-operations script: the place it
actually runs is inside the application image, against the live database. There
the code sits at /app with no installed `vts` package, and running a script BY
PATH puts the script's own directory on `sys.path` — not the repo root. So the
invocation printed in its docstring died on `from vts.core.config import
get_settings` before parsing a single argument.

Measured 2026-09-24, during a real rename: two operators hit it independently
within the hour and each worked around it with `PYTHONPATH=/app`. A workaround
that everyone has to rediscover is a defect in the script, not in the operators
— and the failure lands at the worst moment, on a prod change already begun.

The check is deliberately the SYMPTOM an operator sees — the documented command,
run from somewhere else, with nothing on PYTHONPATH — rather than the presence
of a `sys.path` line. Any fix that makes the command work passes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "merge_user_accounts.py"


def _run_from_elsewhere(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # PYTHONPATH cleared and cwd outside the checkout: that is the image, where
    # neither puts the repo root on the path. Keeping the rest of the
    # environment is deliberate — this is about import resolution, not about
    # running the script in a vacuum.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


def test_the_documented_invocation_gets_past_its_imports(tmp_path):
    result = _run_from_elsewhere(tmp_path, "--help")

    assert "ModuleNotFoundError" not in result.stderr, (
        "the script cannot import its own package when run by path:\n" + result.stderr
    )
    assert result.returncode == 0, result.stderr
    # argparse ran, which is only reachable after every import resolved.
    assert "--move-artifacts" in result.stdout


def test_a_missing_argument_is_refused_by_argparse_not_by_an_import(tmp_path):
    """The other half of the same property, and the one that would rot quietly.

    `--help` short-circuits early enough that a future import added below the
    argument parser would not be covered. Omitting a required argument makes
    argparse exit 2 — proving the parser, not the interpreter, is what stopped
    the run.
    """
    result = _run_from_elsewhere(tmp_path, "--old", "a@old.example")

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == 2, result.stderr
    assert "--new" in result.stderr
