"""Login provider differences (Google / Microsoft Entra ID / generic OIDC) in one place.

One provider per installation, chosen by ``VTS_AUTH_PROVIDER``. Callers (web
login, MCP server, bearer-token path) ask this module instead of naming a
provider, so adding one means touching only this file.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vts.core.config import Settings

GOOGLE_OIDC_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
_ENTRA_METADATA = "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
_MULTI_TENANT = frozenset({"common", "organizations", "consumers"})
_ENTRA_EMAIL_CLAIMS = ("email", "preferred_username", "upn")

# Required settings per provider: (Settings field, environment variable).
_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "google": (
        ("oauth_client_id", "VTS_OAUTH_CLIENT_ID"),
        ("oauth_client_secret", "VTS_OAUTH_CLIENT_SECRET"),
    ),
    "entra": (
        ("entra_tenant_id", "VTS_ENTRA_TENANT_ID"),
        ("entra_client_id", "VTS_ENTRA_CLIENT_ID"),
        ("entra_client_secret", "VTS_ENTRA_CLIENT_SECRET"),
        ("entra_api_scope", "VTS_ENTRA_API_SCOPE"),
    ),
    "oidc": (
        ("oidc_discovery_url", "VTS_OIDC_DISCOVERY_URL"),
        ("oidc_client_id", "VTS_OIDC_CLIENT_ID"),
        ("oidc_client_secret", "VTS_OIDC_CLIENT_SECRET"),
    ),
}


class AuthConfigError(RuntimeError):
    """The chosen login provider is not fully configured."""


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def validate_auth_settings(settings: Settings) -> None:
    """Fail at startup, naming every missing variable, when login is enabled."""
    if not settings.oauth_enabled:
        return
    provider = settings.auth_provider
    missing = [
        env for field, env in _REQUIRED[provider] if _blank(getattr(settings, field))
    ]
    if _blank(settings.public_base_url):
        missing.append("VTS_PUBLIC_BASE_URL")
    if missing:
        raise AuthConfigError(
            f"login is enabled with provider '{provider}' but these settings are "
            f"missing: {', '.join(missing)}"
        )
    if provider == "entra":
        tenant = (settings.entra_tenant_id or "").strip().lower()
        if tenant in _MULTI_TENANT:
            # With the preferred_username/upn email fallback, any tenant's admin
            # could mint a token for an allow-listed address.
            raise AuthConfigError(
                f"VTS_ENTRA_TENANT_ID='{tenant}' accepts accounts from other "
                "organizations; a specific tenant (GUID or verified domain) is required"
            )
        if not settings.entra_api_scope.strip().rpartition("/")[2].strip():
            raise AuthConfigError(
                "VTS_ENTRA_API_SCOPE has an empty scope name; expected e.g. api://<client-id>/vts"
            )


def client_credentials(settings: Settings) -> tuple[str, str]:
    """(client_id, client_secret) of the chosen provider; callers validate first."""
    provider = settings.auth_provider
    if provider == "entra":
        return settings.entra_client_id or "", settings.entra_client_secret or ""
    if provider == "oidc":
        return settings.oidc_client_id or "", settings.oidc_client_secret or ""
    return settings.oauth_client_id or "", settings.oauth_client_secret or ""


def oauth_client_secret(settings: Settings) -> Any:
    """Client secret of the chosen provider (key material for MCP OAuth state)."""
    if getattr(settings, "auth_provider", "google") == "google":
        # Legacy shape: callers holding only the Google fields keep working.
        return settings.oauth_client_secret
    return client_credentials(settings)[1]


def web_oidc_registration(settings: Settings) -> dict[str, Any]:
    """Keyword arguments for authlib's ``OAuth.register`` for the web login."""
    validate_auth_settings(settings)
    provider = settings.auth_provider
    client_id, client_secret = client_credentials(settings)
    if provider == "entra":
        metadata = _ENTRA_METADATA.format(tenant=(settings.entra_tenant_id or "").strip())
        scope = "openid email profile"
    elif provider == "oidc":
        metadata = (settings.oidc_discovery_url or "").strip()
        scope = settings.oidc_scopes
    else:
        metadata = GOOGLE_OIDC_METADATA
        scope = "openid email"
    return {
        "name": provider,
        "client_id": client_id,
        "client_secret": client_secret,
        "server_metadata_url": metadata,
        "client_kwargs": {"scope": scope},
    }


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def email_from_claims(settings: Settings, claims: Mapping[str, Any]) -> str | None:
    """The user's email from ID-token / userinfo claims, or None when absent.

    Trimmed and lower-cased; an empty or non-string value is None, so the
    caller never keys a user by an empty string.
    """
    provider = settings.auth_provider
    if provider == "entra":
        names: tuple[str, ...] = _ENTRA_EMAIL_CLAIMS
    elif provider == "oidc":
        names = (settings.oidc_email_claim,)
    else:
        names = ("email",)
    for name in names:
        email = _clean(claims.get(name))
        if email:
            return email
    return None


def build_mcp_auth_provider(settings: Settings, client_storage: Any) -> Any:
    """The FastMCP auth provider for the chosen login provider.

    Imports are local: the provider classes pull in heavy dependencies and only
    the chosen one is needed.
    """
    validate_auth_settings(settings)
    provider = settings.auth_provider
    client_id, client_secret = client_credentials(settings)
    # base_url stays host-only (no mcp_path suffix) — see build_mcp_server.
    common: dict[str, Any] = {
        "base_url": (settings.public_base_url or "").rstrip("/"),
        "redirect_path": f"{settings.mcp_path.rstrip('/')}/auth/callback",
        "require_authorization_consent": "remember",
        "client_storage": client_storage,
    }
    if provider == "entra":
        from fastmcp.server.auth.providers.azure import AzureProvider

        # "api://<id>/vts" -> identifier "api://<id>", scope name "vts".
        api_scope = (settings.entra_api_scope or "").strip()
        identifier, _, scope_name = api_scope.rpartition("/")
        scope_name = scope_name.strip()
        extra: dict[str, Any] = {"identifier_uri": identifier} if identifier else {}
        return AzureProvider(
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=(settings.entra_tenant_id or "").strip(),
            required_scopes=[scope_name],
            **extra,
            **common,
        )
    if provider == "oidc":
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        return OIDCProxy(
            config_url=(settings.oidc_discovery_url or "").strip(),
            client_id=client_id,
            client_secret=client_secret,
            **common,
        )
    from fastmcp.server.auth.providers.google import GoogleProvider

    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        required_scopes=["openid", "email"],
        **common,
    )
