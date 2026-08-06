"""Validation for user-submitted media URLs (vts-h45).

The worker hands these to yt-dlp from inside the podman network, so an
unvalidated URL is a server-side request primitive: `http://redis:6379/`, the
diarization sidecar, `169.254.169.254`, or any RFC1918 address would be fetched
by us, and the difference between refused / timeout / HTTP status is readable
by the submitter as a network probe.

Deployments may place the worker on a network it shares with unrelated services,
so treat what this module fails to stop as aimed at whatever the worker can
reach, not only at VTS's own sidecars.

Two entry points must both use this: /api/tasks (via TaskCreateRequest) and the
MCP `submit_video` tool, which does not build a TaskCreateRequest at all.

Scope, deliberately: this blocks addresses that are literally internal, and
resolves hostnames to catch names that point inward. It checks the URL the user
submitted, and only that URL. Two ways past it are known and accepted here:

- Redirects (vts-luf4). yt-dlp follows 3xx by default and we set no protocol or
  redirect limits, so a public URL that passes this check can bounce the worker
  to an internal one. Blanket-blocking cross-host redirects is not the fix —
  site -> CDN on another domain is the normal case for real media links.
- DNS rebinding. An attacker-controlled name that resolves publicly at check
  time and privately at connect time.

Both need the check where we cannot put it: at connect time, inside yt-dlp's
socket handling. The real answer to both is network isolation of the download
itself rather than more URL parsing — yt-dlp runs in-process in the worker
(see services/downloader.py), which is why it inherits the worker's full reach.
Tracked separately; until then this module is the proportionate layer for an
OAuth allow-listed user base, not a boundary to rely on.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# One message for every rejection of a resolvable-but-internal target. Saying
# "no such host" for one address and "blocked" for another would hand back the
# very probe signal this validation exists to remove.
_BLOCKED_MESSAGE = (
    "This URL cannot be fetched. Submit a public http(s) media link."
)


class InvalidSourceUrl(ValueError):
    """The submitted URL is not an acceptable media source."""


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not a normal public address.

    `is_global` already covers loopback, private, link-local (including cloud
    metadata at 169.254.169.254), unspecified and reserved ranges; the explicit
    checks below are belt-and-braces for readability and for versions where
    is_global's coverage of a range has shifted.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` resolves to, or [] if it does not resolve.

    All of them are checked: a name resolving to one public and one private
    address must not pass on the strength of the public one.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:  # pragma: no cover - getaddrinfo returned a non-IP
            continue
    return addresses


# Names that are internal by construction rather than by the address they
# happen to resolve to. Checked without DNS so the rule holds offline.
_INTERNAL_NAMES = frozenset({"localhost"})
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".dns.podman",  # podman's per-container aliases, e.g. vts-diarization
)


def _is_internal_name(hostname: str) -> bool:
    name = hostname.rstrip(".").lower()
    if name in _INTERNAL_NAMES:
        return True
    # A bare single label ("redis", "vts-worker") is not routable on the public
    # internet; it can only mean a service on our own network.
    if "." not in name:
        return True
    return name.endswith(_INTERNAL_SUFFIXES)


def validate_source_url(url: str, *, resolve: bool = True) -> str:
    """Return the cleaned URL, or raise InvalidSourceUrl.

    Rejects non-http(s) schemes (uploads build their own file:// URLs
    internally and never come through here) and anything aimed at the host or
    the internal network.

    `resolve=False` skips the DNS lookup, keeping the scheme, literal-address
    and internal-name rules. Tests use it so the suite does not depend on the
    network; production keeps it on, since resolving is what catches a public
    name pointed at a private address.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise InvalidSourceUrl("A URL is required.")

    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise InvalidSourceUrl("This URL could not be parsed.") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidSourceUrl(
            "Only http and https URLs can be submitted "
            f"(got {parts.scheme or 'no'} scheme)."
        )

    try:
        hostname = parts.hostname
    except ValueError as exc:
        # urlsplit defers some malformed-host errors to attribute access.
        raise InvalidSourceUrl("This URL could not be parsed.") from exc
    if not hostname:
        raise InvalidSourceUrl("This URL has no host.")

    # A bare IP in the URL is checked directly; anything else is resolved,
    # which is what catches service names like "redis" on the pod network.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_internal(literal):
            raise InvalidSourceUrl(_BLOCKED_MESSAGE)
        return cleaned

    # A name that looks like an internal service is refused without asking DNS,
    # so the check holds even where the name does not resolve (CI, offline).
    if _is_internal_name(hostname):
        raise InvalidSourceUrl(_BLOCKED_MESSAGE)

    if resolve:
        # Unresolvable is NOT treated as a rejection: yt-dlp resolves again at
        # download time, so a name that fails here may be fine seconds later,
        # and failing closed would reject legitimate links on a DNS blip.
        # Anything that DOES resolve inward is refused.
        if any(_is_internal(ip) for ip in _resolve(hostname)):
            raise InvalidSourceUrl(_BLOCKED_MESSAGE)

    return cleaned
