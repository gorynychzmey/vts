"""The running interpreter must agree with requirements.txt (vts-7to).

A local venv that has drifted from the pins makes local runs lie. That is not
hypothetical: tests passed locally on cryptography 48.0.0 while requirements.txt
pinned 46.0.7, so the HIGH-severity GHSA-537c-gmf6-5ccf in the *pinned* version
was invisible to anyone running the suite. CI caught it only in a separate
workflow that the release path never triggered, and it shipped three times.

This checks the environment actually in use, so a stale venv is reported as a
test failure naming the package rather than as mysteriously-passing tests.
"""
from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

# `name[extra]==1.2.3`, ignoring comments, blank lines, and non-pinned entries
# (`-r`, `-e`, `>=`) which this check has nothing to say about.
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;#]+)")


def _normalize(name: str) -> str:
    """PEP 503 name normalisation, so Foo_Bar and foo-bar compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned_packages() -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _PIN.match(line)
        if match:
            pins.append((match.group("name"), match.group("version")))
    return pins


def test_requirements_file_actually_parses():
    """Guard the guard: a parser that silently matches nothing would make the
    drift check below vacuously green."""
    pins = _pinned_packages()
    assert len(pins) >= 5, f"expected pinned requirements, parsed {len(pins)}"


@pytest.mark.parametrize("name,pinned", _pinned_packages(), ids=lambda v: str(v))
def test_installed_version_matches_the_pin(name: str, pinned: str):
    try:
        installed = version(name)
    except PackageNotFoundError:
        pytest.fail(
            f"{name}=={pinned} is pinned in requirements.txt but is not "
            f"installed in this environment. Run: pip install -r requirements.txt"
        )

    assert _normalize(installed) == _normalize(pinned), (
        f"{name}: requirements.txt pins {pinned}, but {installed} is installed.\n"
        f"Local results do not reflect the pinned dependency — this is how a "
        f"vulnerable pinned version stayed invisible locally (vts-7to).\n"
        f"Run: pip install -r requirements.txt"
    )
