"""Submitted URLs must be http(s) and must not point into the private network.

vts-h45: `url` arrived as `str = Field(min_length=3)` with no other checks and
went straight to yt-dlp in the worker, which runs inside the podman network.
That turns "create a task" into a server-side request primitive: an
authenticated user could aim it at `http://redis:6379/`, the diarization
sidecar, `169.254.169.254`, or any private range, and read back the difference
between refused / timeout / HTTP status as a network probe.

Both entry points are covered on purpose. /api/tasks builds a
TaskCreateRequest, but the MCP `submit_video` tool does NOT — it takes `url`
and passes `url.strip()` to repo.create_task directly, so validating only the
Pydantic schema would leave that path open.
"""
from __future__ import annotations

import pytest

from vts.services.source_url import (
    InvalidSourceUrl,
    validate_source_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "http://example.com/video.mp4",
        "https://example.com:8443/v.mp4",
    ],
)
def test_public_http_urls_are_accepted(url):
    assert validate_source_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://video.mp4",
        "ftp://example.com/v.mp4",
        "gopher://example.com/",
        "data:text/plain;base64,SGVsbG8=",
        "javascript:alert(1)",
        "//example.com/v.mp4",
        "example.com/v.mp4",
    ],
)
def test_non_http_schemes_are_rejected(url):
    """Uploads construct their own file:// URLs internally and never come
    through here, so rejecting them at submission costs nothing."""
    with pytest.raises(InvalidSourceUrl):
        validate_source_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://redis:6379/",                     # named service on the pod network
        "http://127.0.0.1:8080/api/tasks",        # loopback
        "http://localhost:8080/",                 # loopback by name
        "http://[::1]:8080/",                     # loopback, v6
        "http://10.0.0.5/",                       # RFC1918
        "http://192.168.1.1/admin",               # RFC1918
        "http://172.16.0.1/",                     # RFC1918
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0:9100/",                   # unspecified
        "http://[fd00::1]/",                      # unique-local v6
    ],
)
def test_internal_targets_are_rejected(url):
    """The actual SSRF payloads from the report."""
    with pytest.raises(InvalidSourceUrl):
        validate_source_url(url)


def test_rejection_message_does_not_leak_whether_the_host_exists():
    """A message that distinguishes "no such host" from "blocked" would hand
    back exactly the probe signal this validation removes."""
    messages = set()
    for url in ("http://10.0.0.5/", "http://192.168.99.99/"):
        with pytest.raises(InvalidSourceUrl) as excinfo:
            validate_source_url(url)
        messages.add(str(excinfo.value))
    assert len(messages) == 1, messages


def test_empty_and_whitespace_are_rejected():
    for url in ("", "   ", "\n"):
        with pytest.raises(InvalidSourceUrl):
            validate_source_url(url)


def test_hostless_url_is_rejected():
    with pytest.raises(InvalidSourceUrl):
        validate_source_url("http:///video.mp4")


def test_decimal_encoded_loopback_is_rejected():
    """2130706433 == 127.0.0.1. Trivially bypasses a string-prefix check."""
    with pytest.raises(InvalidSourceUrl):
        validate_source_url("http://2130706433/")


def test_the_schema_rejects_internal_urls(monkeypatch):
    """/api/tasks path: TaskCreateRequest must refuse before anything is queued."""
    import pydantic

    from vts.api.schemas import TaskCreateRequest

    with pytest.raises(pydantic.ValidationError):
        TaskCreateRequest(url="http://redis:6379/")

    # And still accepts a normal submission.
    ok = TaskCreateRequest(url="https://example.com/v.mp4")
    assert ok.url == "https://example.com/v.mp4"


@pytest.mark.asyncio
async def test_mcp_submit_video_rejects_internal_urls():
    """MCP path: submit_video bypasses TaskCreateRequest entirely, so it needs
    its own call to the shared validator or the hole stays open there."""
    from fastapi import HTTPException

    from vts.mcp.tools import submit_video

    class _Repo:
        async def create_task(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("task was created for an internal URL")

        async def list_prompts(self, user_id):
            return []

    class _Bus:
        async def notify_queued(self):  # pragma: no cover - must not run
            raise AssertionError("worker was notified for an internal URL")

        async def publish_event(self, **kwargs):  # pragma: no cover
            raise AssertionError("event published for an internal URL")

    class _User:
        id = "00000000-0000-0000-0000-0000000000a1"
        username = "tester"

    with pytest.raises(HTTPException) as excinfo:
        await submit_video(
            url="http://169.254.169.254/latest/meta-data/",
            user=_User(),
            repo=_Repo(),
            bus=_Bus(),
            artifacts_root=__import__("pathlib").Path("/tmp"),
            prompts=[],
        )
    assert excinfo.value.status_code == 422


def test_a_public_name_resolving_inward_is_rejected(monkeypatch):
    """The reason production resolves at all.

    A hostname that looks perfectly ordinary can point at 127.0.0.1 or an
    RFC1918 address; only a lookup catches that. conftest stubs _resolve out
    for the rest of the suite (no network in tests), so this test supplies its
    own answer rather than relying on a real DNS record.
    """
    import ipaddress

    import vts.services.source_url as mod

    monkeypatch.setattr(
        mod, "_resolve", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    with pytest.raises(InvalidSourceUrl):
        validate_source_url("https://totally-normal.example.com/v.mp4")


def test_mixed_resolution_is_rejected(monkeypatch):
    """One public answer must not excuse a private one."""
    import ipaddress

    import vts.services.source_url as mod

    monkeypatch.setattr(
        mod,
        "_resolve",
        lambda host: [
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("10.0.0.5"),
        ],
    )
    with pytest.raises(InvalidSourceUrl):
        validate_source_url("https://mixed.example.com/v.mp4")


def test_unresolvable_host_is_allowed(monkeypatch):
    """Deliberate: failing closed here would reject good links on a DNS blip,
    and yt-dlp resolves again at download time anyway."""
    import vts.services.source_url as mod

    monkeypatch.setattr(mod, "_resolve", lambda host: [])
    assert (
        validate_source_url("https://probably-typo.example.com/v.mp4")
        == "https://probably-typo.example.com/v.mp4"
    )
