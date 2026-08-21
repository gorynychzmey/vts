"""Deny the download child access to private networks (vts-xkx4).

yt-dlp fetches a URL the user supplied, which makes a crafted link an SSRF
primitive: `http://10.88.0.5:4000/…` would have this process fetch a neighbour
on the shared podman network — litellm, ollama, the diarization sidecar — or a
host on the home LAN, and hand the result back. Redirects make it worse, since
validating the URL up front does not survive a 302.

The kernel-level fix (a separate netns, or an nftables rule naming this
process) is not available from inside the container: it has no NET_ADMIN, no
nft binary, and `/proc/sys/net` is read-only. `pasta` was measured and does not
help — it re-issues connections through the HOST stack, where RFC1918 routes
normally, and it has no destination filter. So the enforcement point that IS
available here is this process itself.

Hooking `socket.socket.connect` puts the check below every HTTP library yt-dlp
can pick (requests, urllib3, websockets) rather than in front of one of them,
and checking the ADDRESS at connect time is what closes the two bypasses that
URL inspection cannot:

  * DNS rebinding — by the time `connect()` runs, a name has already become an
    address, so what a hostname resolved to a moment ago does not matter.
  * Redirects — every hop is its own `connect()`, so every hop is checked.

Scope, stated plainly: this covers the process it is installed in. yt-dlp
shells out to ffmpeg for HLS and DASH, and that child does its own fetching
outside this hook — ffmpeg is invoked for specific protocols and container
formats, so what is left is narrow, but it is not nothing. Closing it needs the
kernel-level isolation above, which is a deploy-level change.

There is no port allow-list on purpose. This process downloads and nothing
else; every service the pipeline legitimately needs — Postgres, Redis, whisper,
litellm, the diarization sidecar — is reached by the PARENT. Denying private
space outright is both stricter than an allow-list and has nothing to keep in
sync as neighbours come and go.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

__all__ = ["BlockedAddress", "install_egress_guard", "is_blocked_address"]


class BlockedAddress(OSError):
    """Raised instead of connecting to an address in private space.

    Derives from OSError so callers that already handle connection failures —
    yt-dlp's retry logic among them — treat it as one rather than crashing on
    an exception type they have never heard of.
    """


# Everything that is not the public internet. Loopback and link-local matter as
# much as RFC1918: 127.0.0.1 reaches this container's own services, and
# 169.254.169.254 is the cloud metadata endpoint, the single most-targeted SSRF
# destination there is.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",         # "this host"
        "10.0.0.0/8",        # RFC1918
        "100.64.0.0/10",     # CGNAT / tailscale-style overlays
        "127.0.0.0/8",       # loopback
        "169.254.0.0/16",    # link-local, incl. cloud metadata
        "172.16.0.0/12",     # RFC1918
        "192.0.0.0/24",      # IETF protocol assignments
        "192.168.0.0/16",    # RFC1918
        "198.18.0.0/15",     # benchmarking
        "::1/128",           # IPv6 loopback
        "fc00::/7",          # IPv6 unique-local
        "fe80::/10",         # IPv6 link-local
    )
)

_ORIGINAL_CONNECT = socket.socket.connect


def is_blocked_address(host: str) -> bool:
    """True when `host` is an address inside private space.

    A hostname returns False: this is called with whatever `connect()` was
    given, and by then a name has already been resolved. Anything unparseable
    is likewise left alone rather than guessed at — the guard refuses what it
    can prove, and proving is the whole point.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # An IPv4 address arriving as ::ffff:10.0.0.1 must be judged as the IPv4 it
    # is, or the mapped form walks straight past the v4 networks above.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in _BLOCKED_NETWORKS)


def _guarded_connect(original: Callable[..., Any]) -> Callable[..., Any]:
    def connect(self: Any, address: Any) -> Any:
        # Only (host, port) shapes carry an address to judge. AF_UNIX passes a
        # path string, and raising on a shape this guard does not understand
        # would break unrelated code far from here.
        if isinstance(address, tuple) and address:
            host = address[0]
            if isinstance(host, str) and is_blocked_address(host):
                raise BlockedAddress(
                    f"refusing to connect to {host}: the download step is not "
                    "allowed into private networks (vts-xkx4)"
                )
        return original(self, address)

    return connect


def install_egress_guard() -> Callable[[], None]:
    """Refuse private destinations for the rest of this process.

    Returns a callable that puts the original `connect` back, so tests can
    install and unwind without leaking the hook into the rest of the session.
    Production never calls it: the child exits when the download does.
    """
    previous = socket.socket.connect
    socket.socket.connect = _guarded_connect(previous)  # type: ignore[method-assign]

    def restore() -> None:
        socket.socket.connect = previous  # type: ignore[method-assign]

    return restore
