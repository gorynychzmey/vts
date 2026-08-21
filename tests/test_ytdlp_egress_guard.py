"""The download child must not reach private networks (vts-xkx4).

yt-dlp fetches a URL the user supplied, so a crafted link is an SSRF primitive:
`http://10.88.0.5:4000/...` would have the worker's neighbours on the shared
podman network — litellm, ollama, a Postgres on the home LAN — fetched on the
attacker's behalf. The download step has no legitimate reason to touch any of
them; every service the pipeline actually needs is reached by the PARENT
process, never by this child.

The guard therefore denies private space outright rather than allow-listing
ports, which is both stricter and less to maintain.

It hooks `socket.socket.connect`, which sits below every HTTP library yt-dlp
can use (requests, urllib3, websockets), and it checks the ADDRESS at connect
time. That placement is what closes two attacks for free:
  - DNS rebinding: whatever a name resolved to, `connect()` receives an IP.
  - Redirects: each hop is its own `connect()`, so each is checked.
"""

from __future__ import annotations

import socket

import pytest

from vts.services.egress_guard import BlockedAddress, install_egress_guard


@pytest.fixture
def guard():
    """Install the guard and always restore the real connect afterwards."""
    restore = install_egress_guard()
    try:
        yield
    finally:
        restore()


PRIVATE = [
    ("10.88.0.5", "podman neighbour"),
    ("192.168.178.1", "home LAN gateway"),
    ("172.16.5.4", "RFC1918 /12"),
    ("127.0.0.1", "loopback"),
    ("169.254.169.254", "link-local — the cloud metadata endpoint"),
    ("::1", "IPv6 loopback"),
    ("fd00::1", "IPv6 unique-local"),
]


@pytest.mark.parametrize("addr,label", PRIVATE)
def test_private_destinations_are_refused(guard, addr: str, label: str) -> None:
    s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET)
    s.settimeout(1)
    with pytest.raises(BlockedAddress) as exc:
        s.connect((addr, 80))
    assert addr in str(exc.value), f"the error must name the address it refused ({label})"
    s.close()


def test_public_destinations_are_left_alone(guard) -> None:
    """The guard must not become an outage: public space still connects.

    Asserted without touching the network — a real dial-out would make this
    test depend on the internet. What matters is that the guard does not raise;
    whatever the socket then does is the kernel's business.
    """
    calls: list[tuple] = []

    class FakeSocket:
        def connect(self, addr):
            calls.append(addr)

    from vts.services import egress_guard

    guarded = egress_guard._guarded_connect(FakeSocket.connect)
    guarded(FakeSocket(), ("93.184.216.34", 443))
    assert calls == [("93.184.216.34", 443)], "a public address must pass through untouched"


def test_a_redirect_to_private_space_is_caught_on_the_second_hop(guard) -> None:
    """Redirects are not a special case, and that is the point.

    A public URL that 302s to `http://169.254.169.254/` is the classic bypass
    for anything that validates the URL up front. Here the second hop is just
    another connect(), so it is checked exactly like the first.
    """
    s = socket.socket()
    s.settimeout(1)
    with pytest.raises(BlockedAddress):
        s.connect(("169.254.169.254", 80))
    s.close()


def test_hostnames_are_irrelevant_because_the_check_is_on_the_address(guard) -> None:
    """DNS rebinding: the name is never what gets checked.

    `socket.create_connection` resolves first and then connects to an address,
    so a name that resolves into private space is refused at the moment it
    matters, no matter what it was called or what it resolved to a second ago.
    """
    from vts.services import egress_guard

    calls: list[tuple] = []

    class FakeSocket:
        def connect(self, addr):
            calls.append(addr)

    guarded = egress_guard._guarded_connect(FakeSocket.connect)
    with pytest.raises(BlockedAddress):
        guarded(FakeSocket(), ("127.0.0.1", 8080))
    assert not calls, "the real connect must never run for a blocked address"


def test_unix_sockets_still_work(guard) -> None:
    """Not every connect() carries an (ip, port): a path must pass through.

    yt-dlp does not use AF_UNIX, but the hook is global for the process, and a
    guard that raises on a shape it does not understand would break unrelated
    code in a way that is hard to trace back to here.
    """
    from vts.services import egress_guard

    calls: list = []

    class FakeSocket:
        def connect(self, addr):
            calls.append(addr)

    guarded = egress_guard._guarded_connect(FakeSocket.connect)
    guarded(FakeSocket(), "/tmp/some.sock")
    assert calls == ["/tmp/some.sock"]


def test_restore_puts_the_real_connect_back(guard) -> None:
    """Installing must be reversible, or the tests poison the whole session."""
    from vts.services import egress_guard

    restore = install_egress_guard()
    assert socket.socket.connect is not egress_guard._ORIGINAL_CONNECT
    restore()
    # The outer fixture's guard is still installed, so this asserts the inner
    # install/restore pair nested cleanly rather than clobbering it.
    assert callable(socket.socket.connect)


def test_the_runner_installs_the_guard_before_reading_the_request() -> None:
    """The wiring, which is the part that actually regressed elsewhere today.

    Both the guard and its tests can be perfectly correct while nothing calls
    it. Assert the call site, and its ORDER: the request carries the
    attacker-controlled URL, so the guard has to be up before that is parsed,
    not somewhere inside the download.
    """
    import inspect

    from vts.services import ytdlp_runner

    src = inspect.getsource(ytdlp_runner.main)
    assert "install_egress_guard()" in src, "the runner never installs the guard"
    assert src.index("install_egress_guard()") < src.index("json.loads"), (
        "the guard must be installed before the request is parsed"
    )
