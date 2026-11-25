"""Pluggable login provider: settings, validation, web registration, email, MCP provider."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from vts.core.config import Settings
from vts.services import auth_providers
from vts.services.auth_providers import (
    AuthConfigError,
    build_mcp_auth_provider,
    email_from_claims,
    validate_auth_settings,
    web_oidc_registration,
)

BASE = "https://vts.example.com"


def _s(**kw) -> Settings:
    kw.setdefault("oauth_enabled", True)
    kw.setdefault("public_base_url", BASE)
    return Settings(**kw)


def _google(**kw) -> Settings:
    return _s(oauth_client_id="g-id", oauth_client_secret="g-secret", **kw)


def _entra(**kw) -> Settings:
    kw.setdefault("entra_api_scope", "api://app-id/vts")
    base = {
        "auth_provider": "entra",
        "entra_tenant_id": "tenant-1",
        "entra_client_id": "e-id",
        "entra_client_secret": "e-secret",
    }
    return _s(**{**base, **kw})


def _oidc(**kw) -> Settings:
    return _s(
        auth_provider="oidc",
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        oidc_client_id="o-id",
        oidc_client_secret="o-secret",
        **kw,
    )


# --- settings -----------------------------------------------------------------


def test_defaults_keep_google(monkeypatch) -> None:
    for name in ("VTS_AUTH_PROVIDER", "VTS_OIDC_EMAIL_CLAIM", "VTS_OIDC_SCOPES"):
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.auth_provider == "google"
    assert s.oidc_email_claim == "email"
    assert s.oidc_scopes == "openid email profile"
    assert s.entra_tenant_id is None and s.oidc_discovery_url is None


def test_env_names_are_read(monkeypatch) -> None:
    monkeypatch.setenv("VTS_AUTH_PROVIDER", "entra")
    monkeypatch.setenv("VTS_ENTRA_TENANT_ID", "t")
    monkeypatch.setenv("VTS_ENTRA_CLIENT_ID", "c")
    monkeypatch.setenv("VTS_ENTRA_CLIENT_SECRET", "s")
    monkeypatch.setenv("VTS_ENTRA_API_SCOPE", "api://c/vts")
    monkeypatch.setenv("VTS_OIDC_DISCOVERY_URL", "https://i/.well-known/openid-configuration")
    monkeypatch.setenv("VTS_OIDC_CLIENT_ID", "oc")
    monkeypatch.setenv("VTS_OIDC_CLIENT_SECRET", "os")
    monkeypatch.setenv("VTS_OIDC_EMAIL_CLAIM", "mail")
    monkeypatch.setenv("VTS_OIDC_SCOPES", "openid mail")
    s = Settings()
    assert (s.auth_provider, s.entra_tenant_id, s.entra_client_id) == ("entra", "t", "c")
    assert (s.entra_client_secret, s.entra_api_scope) == ("s", "api://c/vts")
    assert s.oidc_discovery_url == "https://i/.well-known/openid-configuration"
    assert (s.oidc_client_id, s.oidc_client_secret) == ("oc", "os")
    assert (s.oidc_email_claim, s.oidc_scopes) == ("mail", "openid mail")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_provider="github")


# --- validation ---------------------------------------------------------------


def test_validation_is_skipped_when_oauth_disabled() -> None:
    validate_auth_settings(Settings(oauth_enabled=False, auth_provider="entra"))


def test_validate_google_ok_and_missing() -> None:
    validate_auth_settings(_google())
    with pytest.raises(AuthConfigError) as exc:
        validate_auth_settings(_s(public_base_url=None))
    msg = str(exc.value)
    for name in ("VTS_OAUTH_CLIENT_ID", "VTS_OAUTH_CLIENT_SECRET", "VTS_PUBLIC_BASE_URL"):
        assert name in msg


def test_validate_entra_ok_and_names_every_missing_variable() -> None:
    validate_auth_settings(_entra())
    with pytest.raises(AuthConfigError) as exc:
        validate_auth_settings(_s(auth_provider="entra", public_base_url=None))
    msg = str(exc.value)
    for name in (
        "VTS_ENTRA_TENANT_ID",
        "VTS_ENTRA_CLIENT_ID",
        "VTS_ENTRA_CLIENT_SECRET",
        "VTS_ENTRA_API_SCOPE",
        "VTS_PUBLIC_BASE_URL",
    ):
        assert name in msg
    assert "VTS_OAUTH_CLIENT_ID" not in msg


def test_validate_oidc_ok_and_names_every_missing_variable() -> None:
    validate_auth_settings(_oidc())
    with pytest.raises(AuthConfigError) as exc:
        validate_auth_settings(_s(auth_provider="oidc"))
    msg = str(exc.value)
    for name in ("VTS_OIDC_DISCOVERY_URL", "VTS_OIDC_CLIENT_ID", "VTS_OIDC_CLIENT_SECRET"):
        assert name in msg


def test_blank_values_count_as_missing() -> None:
    with pytest.raises(AuthConfigError, match="VTS_ENTRA_TENANT_ID"):
        validate_auth_settings(_entra(entra_tenant_id="  "))


def test_auth_config_error_is_a_runtime_error() -> None:
    assert issubclass(AuthConfigError, RuntimeError)


# --- web registration -----------------------------------------------------------


def test_web_registration_google() -> None:
    reg = web_oidc_registration(_google())
    assert reg == {
        "name": "google",
        "client_id": "g-id",
        "client_secret": "g-secret",
        "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
        "client_kwargs": {"scope": "openid email"},
    }


def test_web_registration_entra() -> None:
    reg = web_oidc_registration(_entra())
    assert reg["name"] == "entra"
    assert reg["client_id"] == "e-id" and reg["client_secret"] == "e-secret"
    assert reg["server_metadata_url"] == (
        "https://login.microsoftonline.com/tenant-1/v2.0/.well-known/openid-configuration"
    )
    assert reg["client_kwargs"] == {"scope": "openid email profile"}


def test_web_registration_oidc_uses_configured_scopes() -> None:
    reg = web_oidc_registration(_oidc(oidc_scopes="openid mail"))
    assert reg["name"] == "oidc"
    assert reg["server_metadata_url"] == (
        "https://idp.example.com/.well-known/openid-configuration"
    )
    assert reg["client_id"] == "o-id" and reg["client_secret"] == "o-secret"
    assert reg["client_kwargs"] == {"scope": "openid mail"}


# --- email from claims ------------------------------------------------------------


def test_google_email_is_trimmed_and_lowercased() -> None:
    assert email_from_claims(_google(), {"email": "  Ann@Example.COM "}) == "ann@example.com"


def test_google_ignores_other_claims() -> None:
    assert email_from_claims(_google(), {"preferred_username": "a@b.c"}) is None


def test_entra_prefers_email_then_preferred_username_then_upn() -> None:
    s = _entra()
    assert email_from_claims(s, {"email": "E@x.com", "preferred_username": "p@x.com", "upn": "u@x.com"}) == "e@x.com"
    assert email_from_claims(s, {"preferred_username": " P@X.com ", "upn": "u@x.com"}) == "p@x.com"
    assert email_from_claims(s, {"upn": "U@X.com"}) == "u@x.com"


def test_entra_blank_email_falls_through() -> None:
    assert email_from_claims(_entra(), {"email": "  ", "preferred_username": "p@x.com"}) == "p@x.com"


def test_entra_without_any_address_is_none() -> None:
    assert email_from_claims(_entra(), {"sub": "abc", "email": ""}) is None


def test_oidc_uses_configured_claim() -> None:
    s = _oidc(oidc_email_claim="mail")
    assert email_from_claims(s, {"mail": "M@Corp.de", "email": "other@x.com"}) == "m@corp.de"


def test_oidc_missing_or_empty_custom_claim_is_none_never_empty_string() -> None:
    s = _oidc(oidc_email_claim="mail")
    assert email_from_claims(s, {"email": "other@x.com"}) is None
    assert email_from_claims(s, {"mail": "   "}) is None
    assert email_from_claims(s, {"mail": None}) is None


def test_non_string_claim_is_none() -> None:
    assert email_from_claims(_google(), {"email": 42}) is None
    assert email_from_claims(_google(), {"email": ["a@b.c"]}) is None


# --- MCP provider -------------------------------------------------------------------

STORAGE = object()


class _Capture:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _patch(monkeypatch, module: str, cls: str) -> None:
    monkeypatch.setattr(f"{module}.{cls}", _Capture)


def test_mcp_google_arguments_are_unchanged(monkeypatch) -> None:
    _patch(monkeypatch, "fastmcp.server.auth.providers.google", "GoogleProvider")
    p = build_mcp_auth_provider(_google(), STORAGE)
    assert isinstance(p, _Capture)
    assert p.kwargs == {
        "client_id": "g-id",
        "client_secret": "g-secret",
        "base_url": BASE,
        "redirect_path": "/mcp/auth/callback",
        "required_scopes": ["openid", "email"],
        "require_authorization_consent": "remember",
        "client_storage": STORAGE,
    }


def test_mcp_base_url_slash_and_mcp_path_are_normalised(monkeypatch) -> None:
    _patch(monkeypatch, "fastmcp.server.auth.providers.google", "GoogleProvider")
    p = build_mcp_auth_provider(_google(public_base_url=BASE + "/", mcp_path="/api/mcp/"), STORAGE)
    assert p.kwargs["base_url"] == BASE
    assert p.kwargs["redirect_path"] == "/api/mcp/auth/callback"


def test_mcp_entra_splits_the_api_scope(monkeypatch) -> None:
    _patch(monkeypatch, "fastmcp.server.auth.providers.azure", "AzureProvider")
    p = build_mcp_auth_provider(_entra(), STORAGE)
    assert p.kwargs == {
        "client_id": "e-id",
        "client_secret": "e-secret",
        "tenant_id": "tenant-1",
        "required_scopes": ["vts"],
        "identifier_uri": "api://app-id",
        "base_url": BASE,
        "redirect_path": "/mcp/auth/callback",
        "require_authorization_consent": "remember",
        "client_storage": STORAGE,
    }


def test_mcp_entra_scope_without_slash_leaves_identifier_to_the_provider(monkeypatch) -> None:
    _patch(monkeypatch, "fastmcp.server.auth.providers.azure", "AzureProvider")
    p = build_mcp_auth_provider(_entra(entra_api_scope="vts"), STORAGE)
    assert p.kwargs["required_scopes"] == ["vts"]
    assert "identifier_uri" not in p.kwargs


def test_mcp_oidc_arguments(monkeypatch) -> None:
    _patch(monkeypatch, "fastmcp.server.auth.oidc_proxy", "OIDCProxy")
    p = build_mcp_auth_provider(_oidc(), STORAGE)
    assert p.kwargs == {
        "config_url": "https://idp.example.com/.well-known/openid-configuration",
        "client_id": "o-id",
        "client_secret": "o-secret",
        "base_url": BASE,
        "redirect_path": "/mcp/auth/callback",
        "require_authorization_consent": "remember",
        "client_storage": STORAGE,
    }


def test_mcp_builder_validates_first() -> None:
    with pytest.raises(AuthConfigError, match="VTS_ENTRA_TENANT_ID"):
        build_mcp_auth_provider(_s(auth_provider="entra"), STORAGE)


def test_module_exposes_google_metadata_constant() -> None:
    assert auth_providers.GOOGLE_OIDC_METADATA.startswith("https://accounts.google.com/")


def test_real_azure_provider_accepts_our_arguments() -> None:
    """Signature drift guard: the real class, no network, no capture stub."""
    p = build_mcp_auth_provider(_entra(), None)
    assert type(p).__name__ == "AzureProvider"


def test_real_google_provider_accepts_our_arguments() -> None:
    p = build_mcp_auth_provider(_google(), None)
    assert type(p).__name__ == "GoogleProvider"
