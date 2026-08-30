"""The configuration reference must name keys that actually work.

Writing `VTS_SERVICES_SEARCH_THRESHOLD` into ARCHITECTURE.md is an easy and
invisible mistake: the YAML path is `services.search_threshold`, so the env
name looks like it should carry the prefix too — but Settings uses
`env_prefix="VTS_"` plus the FIELD name, giving `VTS_SEARCH_THRESHOLD`. Both
forms read as plausible, and a wrong one fails silently: the operator sets it,
nothing changes, and the default keeps working.

This checks the documented env names against the model rather than against
someone's memory of it.
"""
from __future__ import annotations

import re
from pathlib import Path

from vts.core.config import Settings

_DOC = Path(__file__).resolve().parents[1] / "docs" / "ARCHITECTURE.md"
# | `yaml.path` | `VTS_ENV_NAME` | `default` |
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`(VTS_[A-Z0-9_]+)`\s*\|", re.M)


# Documented keys that correspond to no Settings field. Each needs its own
# issue and a reason — an entry here means "known and tracked", never "ignore".
_KNOWN_MISMATCHES = {
    # vts-4g94: neither the field nor any reader exists; the row may describe an
    # intention rather than a fact, so it is not silently deleted.
    "VTS_TRUSTED_PROXY_CIDRS",
}


def test_every_documented_env_name_maps_to_a_real_setting():
    fields = set(Settings.model_fields)
    documented = _ROW.findall(_DOC.read_text(encoding="utf-8"))
    assert documented, "no key rows found — did the reference table change shape?"

    unknown = []
    for yaml_path, env_name in documented:
        if env_name in _KNOWN_MISMATCHES:
            continue
        field = env_name[len("VTS_"):].lower()
        if field not in fields:
            unknown.append((yaml_path, env_name))

    assert not unknown, (
        "these documented env names do not correspond to any Settings field, "
        "so setting them would silently do nothing:\n  "
        + "\n  ".join(f"{y} -> {e}" for y, e in unknown)
    )


def test_the_search_and_embedding_keys_are_documented():
    # The keys added with corpus search; an operator has to be able to find
    # them without reading the source.
    documented = {env for _, env in _ROW.findall(_DOC.read_text(encoding="utf-8"))}
    for expected in ("VTS_SEARCH_THRESHOLD", "VTS_EMBEDDING_MODEL", "VTS_EMBEDDING_ENABLED"):
        assert expected in documented, f"{expected} is not in the configuration reference"


def test_the_known_mismatches_are_still_mismatches():
    """Stop the exception list from outliving the problem.

    Once vts-4g94 is resolved — the row removed, or the setting implemented —
    this fails and the entry has to go, instead of quietly excusing a key that
    now works.
    """
    fields = set(Settings.model_fields)
    for env_name in _KNOWN_MISMATCHES:
        field = env_name[len("VTS_"):].lower()
        assert field not in fields, (
            f"{env_name} now maps to a real setting — remove it from "
            f"_KNOWN_MISMATCHES"
        )
