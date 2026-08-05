"""Plugins still work against the contract in this checkout (vts-j8gz).

The delivery contract is a public interface. Plugins live in a separate
repository (gorynychzmey/vts-plugins), are built against this contract, and
bind to it at load time — so a change here can break a plugin that nobody
rebuilt. In production that surfaces as a delivery which quietly stops
working, not as a red build.

This runs on OUR side, before the change is pushed, rather than from CI: a
workflow in vts cannot dispatch to another repository, because the built-in
GITHUB_TOKEN is scoped to the repo running it (verified: HTTP 403 "Resource
not accessible by integration"). Checking here needs no cross-repo token at
all and fails while the change is still cheap to fix.

SKIPPED when the plugin repo is not checked out beside vts: a VTS checkout is
perfectly usable without it, and this must never block someone who does not
touch plugins. The plugin repo runs the same check in its own CI, nightly, so
a machine without the checkout is not a hole.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_plugins_against_contract.sh"
PLUGINS_DIR = Path(
    os.environ.get("VTS_PLUGINS_DIR", Path.home() / "dev" / "vts-plugins")
)


pytestmark = pytest.mark.skipif(
    not PLUGINS_DIR.is_dir(),
    reason=f"no plugin checkout at {PLUGINS_DIR}; the plugin repo's own CI covers this",
)


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


@pytest.mark.slow
def test_plugins_pass_against_this_contract():
    """Build the contract from THIS tree and run every plugin's tests on it.

    Deliberately uses the working tree rather than the published package —
    the whole point is to test the contract change that has not shipped yet.
    """
    result = subprocess.run(
        [str(SCRIPT), str(PLUGINS_DIR)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, (
        "a plugin is broken by the contract in this checkout — fix the plugin "
        f"in {PLUGINS_DIR} (and release it) before shipping this change:\n"
        f"{result.stdout}\n{result.stderr}"
    )
