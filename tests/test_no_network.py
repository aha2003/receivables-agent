"""No network anywhere in the test suite (SPEC §3.5; D-029).

``tests/network_guard.py`` is installed for the whole ``pytest -q`` run. These tests prove it is
on, that every route to the network is refused (sockets, DNS, httpx, urllib), that local Unix
sockets still work, and that an attempt which a test swallows still fails the run.
"""

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import httpx
import network_guard
import pytest
from network_guard import NetworkBlocked, expected_block

SWALLOW_PROBE = "RECEIVABLES_SWALLOW_PROBE"


def test_the_guard_is_installed_for_the_whole_run() -> None:
    assert network_guard.installed()
    assert network_guard.unexpected() == []


@pytest.mark.parametrize(
    ("family", "kind", "address"),
    [
        (socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 9)),
        (socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 9)),
        (socket.AF_INET6, socket.SOCK_STREAM, ("::1", 9)),
    ],
)
def test_sockets_cannot_connect(family: int, kind: int, address: tuple[str, int]) -> None:
    with expected_block(), socket.socket(family, kind) as sock:
        with pytest.raises(NetworkBlocked):
            sock.connect(address)
        with pytest.raises(NetworkBlocked):
            sock.connect_ex(address)
        with pytest.raises(NetworkBlocked):
            sock.sendto(b"x", address)
        with pytest.raises(NetworkBlocked):
            sock.sendto(b"x", 0, address)
        if hasattr(sock, "sendmsg"):
            with pytest.raises(NetworkBlocked):
                sock.sendmsg([b"x"], [], 0, address)


def test_dns_and_helpers_are_blocked() -> None:
    with expected_block():
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("api.anthropic.com", 443)
        with pytest.raises(NetworkBlocked):
            socket.gethostbyname("api.anthropic.com")
        with pytest.raises(NetworkBlocked):
            socket.gethostbyname_ex("api.anthropic.com")
        with pytest.raises(NetworkBlocked):
            socket.gethostbyaddr("192.0.2.1")
        with pytest.raises(NetworkBlocked):
            socket.getnameinfo(("192.0.2.1", 443), 0)
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("api.anthropic.com", 443))


def test_http_clients_are_blocked() -> None:
    with expected_block():
        with pytest.raises(NetworkBlocked):
            httpx.get("https://api.anthropic.com/v1/messages", timeout=1)
        with pytest.raises(NetworkBlocked):
            urllib.request.urlopen("https://api.anthropic.com/", timeout=1)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no Unix-domain sockets")
def test_local_unix_sockets_still_work() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"ping")
        assert right.recv(4) == b"ping"
        left.sendmsg([b"pong"])
        assert right.recv(4) == b"pong"
    # A short path: macOS limits Unix socket paths to about 100 characters.
    with tempfile.TemporaryDirectory(prefix="ng", dir="/tmp") as folder:
        path = str(Path(folder) / "s")
        with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
            server.bind(path)
            server.listen(1)
            client.connect(path)
    assert network_guard.unexpected() == []


@pytest.mark.skipif(os.environ.get(SWALLOW_PROBE) != "1", reason="only run by the test below")
def test_probe_that_swallows_a_network_error() -> None:
    with contextlib.suppress(Exception):  # deliberately swallowed: the guard must still notice
        socket.create_connection(("api.anthropic.com", 443))


def test_a_swallowed_attempt_still_fails_the_run() -> None:
    target = f"{Path(__file__).resolve()}::test_probe_that_swallows_a_network_error"
    done = subprocess.run(  # noqa: S603 - a fixed command: this test file under pytest
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        env={**os.environ, SWALLOW_PROBE: "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert "1 passed" in done.stdout  # the probe itself passed...
    assert done.returncode == pytest.ExitCode.TESTS_FAILED  # ...but the run failed
    assert "network guard: 1 blocked network attempt" in done.stdout


def test_uninstall_restores_and_install_is_idempotent() -> None:
    connect, sendto = socket.socket.connect, socket.socket.sendto
    network_guard.install()  # already installed: no double wrapping
    assert (socket.socket.connect, socket.socket.sendto) == (connect, sendto)
    network_guard.uninstall()
    try:
        assert not network_guard.installed()
        for target in (socket.socket.connect, socket.socket.sendto, socket.getnameinfo):
            assert not getattr(target, "network_guard", False)
    finally:
        network_guard.install()
    assert network_guard.installed()
