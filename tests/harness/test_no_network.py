"""The network guard — the reason "zero network calls" is a property.

S5's verification says a full workflow runs with zero network calls. The only
honest way to have that is to make one impossible rather than to assert
afterwards that none happened, so these tests are about the guard itself.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tests.conftest import NetworkBlocked


def test_tcp_is_blocked() -> None:
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.invalid", 80))


def test_dns_is_blocked() -> None:
    """A name lookup is the first observable step of most accidental egress,
    so blocking it points the failure at the call that meant to go out."""
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.invalid", 80)


def test_connecting_an_inet_socket_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect(("127.0.0.1", 9))


def test_connect_ex_is_blocked_too() -> None:
    """It reports errors as a return code rather than an exception, which is
    exactly the shape a scanner or a health check uses — and would otherwise
    slip past a guard that only patched ``connect``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect_ex(("127.0.0.1", 9))


def test_ipv6_is_blocked() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect(("::1", 9))


def test_the_failure_says_what_to_do_instead() -> None:
    """It will be read by somebody who did not write this file, halfway through
    adding an adapter."""
    with pytest.raises(NetworkBlocked) as caught:
        socket.create_connection(("example.invalid", 80))
    message = str(caught.value)
    assert "clawdence.ports" in message
    assert "cassette" in message
    assert "allow_network" in message


def test_unix_sockets_are_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Local IPC is not egress, and from S7 the Docker daemon socket is how a
    container gets started at all. Blocking it would make the guard the reason
    the runner tests cannot run.

    Bound relative to the working directory because an ``AF_UNIX`` path is
    capped at about 100 bytes, and a pytest ``tmp_path`` is longer than that on
    macOS — an implementation detail of the test, not of the guard.
    """
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind("s.sock")
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect("s.sock")


@pytest.mark.allow_network
def test_the_marker_lifts_the_guard() -> None:
    """The escape hatch works, and using it is visible in the test file.

    Connecting to a closed local port raises ``ConnectionRefusedError`` — an
    ordinary ``OSError``, which is precisely the point: the guard is gone, so
    the failure comes from the network stack rather than from us.
    """
    with pytest.raises(OSError) as caught:
        socket.create_connection(("127.0.0.1", 9), timeout=0.5)
    assert not isinstance(caught.value, NetworkBlocked)
