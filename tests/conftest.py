from __future__ import annotations

# ---------------------------------------------------------------------------
# Guard for dev/test environments where the host config.yaml enables OAuth
# but does not supply mcp_oauth_client_secret (which is a runtime secret,
# not stored in YAML).
#
# build_mcp_server() raises RuntimeError when mcp_oauth_enabled=True and the
# secret is absent.  Patching _load_yaml_overrides to inject a placeholder
# satisfies the guard for tests that go through get_settings() (which calls
# _load_yaml_overrides internally).
#
# This conftest lives in tests/ rather than the project root so it is scoped
# to the test suite only — not a global side-effect on any pytest invocation
# in the repo root.
#
# The test_webapi_does_not_mount_mcp_when_disabled known failure (vts-3ur) is
# intentionally preserved: that test fails because the yaml also supplies
# mcp_enabled=True as a constructor kwarg which beats the env var — a separate
# isolation problem tracked in vts-3ur.
# ---------------------------------------------------------------------------

try:
    import vts.core.config as _cfg

    _original_load_yaml_overrides = _cfg._load_yaml_overrides

    def _patched_load_yaml_overrides() -> dict:
        overrides = _original_load_yaml_overrides()
        if overrides.get("mcp_oauth_enabled") and not overrides.get("mcp_oauth_client_secret"):
            overrides = dict(overrides)
            overrides["mcp_oauth_client_secret"] = "test-placeholder-secret"
        return overrides

    _cfg._load_yaml_overrides = _patched_load_yaml_overrides  # type: ignore[assignment]
except Exception:
    pass
