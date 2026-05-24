from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from vts.core.config import Settings

GOOGLE_OIDC_METADATA = "https://accounts.google.com/.well-known/openid-configuration"


def build_oauth_client(settings: Settings) -> OAuth:
    if not settings.oauth_enabled:
        raise RuntimeError("oauth_enabled is False — won't construct an OAuth client")
    if not settings.oauth_client_id or not settings.oauth_client_secret:
        raise RuntimeError("oauth_client_id/secret are required when oauth_enabled=True")

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        server_metadata_url=GOOGLE_OIDC_METADATA,
        client_kwargs={"scope": "openid email"},
    )
    return oauth
