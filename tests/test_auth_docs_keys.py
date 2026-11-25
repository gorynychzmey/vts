"""Every auth variable named in docs/AUTH.md is a real setting (vts-sf93.1)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from vts.core.config import Settings

_DOC = Path(__file__).resolve().parent.parent / "docs" / "AUTH.md"
_PROVIDER_KEYS = re.compile(r"VTS_(?:AUTH_PROVIDER|ENTRA_[A-Z_]+|OIDC_[A-Z_]+)")


def _documented() -> list[str]:
    return sorted(set(_PROVIDER_KEYS.findall(_DOC.read_text(encoding="utf-8"))))


def test_doc_names_the_expected_variables() -> None:
    assert _documented() == [
        "VTS_AUTH_PROVIDER",
        "VTS_ENTRA_API_SCOPE",
        "VTS_ENTRA_CLIENT_ID",
        "VTS_ENTRA_CLIENT_SECRET",
        "VTS_ENTRA_TENANT_ID",
        "VTS_OIDC_CLIENT_ID",
        "VTS_OIDC_CLIENT_SECRET",
        "VTS_OIDC_DISCOVERY_URL",
        "VTS_OIDC_EMAIL_CLAIM",
        "VTS_OIDC_SCOPES",
    ]


@pytest.mark.parametrize("env_name", _documented())
def test_documented_variable_loads_into_settings(env_name: str, monkeypatch) -> None:
    value = "entra" if env_name == "VTS_AUTH_PROVIDER" else "sentinel-value"
    monkeypatch.setenv(env_name, value)
    settings = Settings()
    loaded = [v for k, v in settings.model_dump().items() if v == value]
    assert loaded == [value], f"{env_name} did not reach any Settings field"
