"""The contract package is buildable and self-sufficient (vts-j8gz).

Plugins live in their own repository (gorynychzmey/vts-plugins) and cannot
`pip install vts` — VTS is a source tree, not a distribution. They depend on
`packages/vts-delivery-contract` instead, which force-includes the contract
module from this tree rather than keeping a second copy.

That arrangement has two ways to break silently, and both are checked here:
the packaging config can stop matching the module's real location, and the
contract can acquire an import that the standalone package does not carry.
Either one is only discovered when a plugin build fails in the OTHER repo,
which is a slow and confusing place to find out.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "vts-delivery-contract"
CONTRACT = REPO / "vts" / "delivery" / "contract.py"


def test_packaging_points_at_the_real_contract_module():
    """The force-include paths must resolve. A rename in the core would
    otherwise leave a config that builds an EMPTY wheel — pip installs it
    happily and the plugin fails later with ImportError."""
    config = tomllib.loads((PKG / "pyproject.toml").read_text(encoding="utf-8"))
    included = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert included, "wheel force-include must not be empty"
    for source, target in included.items():
        resolved = (PKG / source).resolve()
        assert resolved.exists(), f"force-include source is missing: {source}"
        assert resolved.is_relative_to(REPO), f"force-include escapes the repo: {source}"
        # The module must land at its real import path, so plugin code needs
        # no rewriting when it moves from in-tree to installed.
        assert target.startswith("vts/delivery/"), (
            f"contract must install under vts/delivery/, got {target}"
        )
    assert str(CONTRACT.relative_to(REPO)) in included.values()


def test_version_matches_the_contract_it_ships():
    """The package version tracks CONTRACT_VERSION. If they disagree, a plugin
    pinning `>=1.1,<2` can silently receive a contract that is not 1.1."""
    from vts.delivery.contract import CONTRACT_VERSION

    config = tomllib.loads((PKG / "pyproject.toml").read_text(encoding="utf-8"))
    version = config["project"]["version"]
    major, minor = version.split(".")[:2]
    assert (int(major), int(minor)) == CONTRACT_VERSION, (
        f"package version {version} does not match CONTRACT_VERSION "
        f"{CONTRACT_VERSION} — bump packages/vts-delivery-contract/pyproject.toml"
    )


def test_contract_imports_nothing_from_the_rest_of_vts():
    """The package ships ONLY the contract module. An import reaching into
    vts.db, vts.services, … would make the wheel installable but broken for
    every plugin — and the failure would surface in the plugin repo, not here.
    """
    source = CONTRACT.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import vts", "from vts"))
        and "vts.delivery.contract" not in line
    ]
    assert not offenders, (
        "the contract must stand alone, but it imports from the wider "
        f"codebase: {offenders}"
    )


def _pip_importable() -> bool:
    """`pip` is invoked as `python -m pip`, so the MODULE is what matters.
    Checking for a `pip` binary on PATH would skip this test on any venv that
    does not expose one — which is how it silently skipped at first."""
    return importlib.util.find_spec("pip") is not None


@pytest.mark.skipif(not _pip_importable(), reason="pip module not importable")
def test_wheel_builds_and_carries_the_contract(tmp_path):
    """End-to-end: the wheel actually builds and contains the module.

    Slower than the checks above, but it is the only one that would catch a
    hatchling behaviour change, and a broken build here means every plugin's
    CI breaks at once.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(tmp_path), str(PKG)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    names = zipfile.ZipFile(wheels[0]).namelist()
    assert "vts/delivery/contract.py" in names, (
        f"the built wheel does not carry the contract: {names}"
    )
