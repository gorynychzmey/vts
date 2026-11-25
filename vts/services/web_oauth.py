from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from vts.core.config import Settings
from vts.services.auth_providers import GOOGLE_OIDC_METADATA, web_oidc_registration

__all__ = ["GOOGLE_OIDC_METADATA", "build_oauth_client"]


def build_oauth_client(settings: Settings) -> OAuth:
    if not settings.oauth_enabled:
        raise RuntimeError("oauth_enabled is False — won't construct an OAuth client")

    oauth = OAuth()
    oauth.register(**web_oidc_registration(settings))
    return oauth
