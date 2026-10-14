"""Network guard for the whole test suite (SPEC §3.5 "Tests never call the real API"; D-029).

Installed by ``tests/conftest.py`` at session start, before any test module is imported, and
removed at session end. Every TCP/UDP connection and every DNS lookup raises ``NetworkBlocked``;
Unix-domain sockets stay allowed (local IPC, not the network). Each refused attempt is counted,
so an attempt that some library catches and swallows still fails the run, unless a test
deliberately expects it inside ``expected_block()``.
"""

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["NetworkBlocked", "expected_block", "install", "installed", "unexpected", "uninstall"]


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network. Not an ``OSError``, so clients cannot retry it away."""


_ORIGINAL: dict[str, Any] = {}
_UNEXPECTED: list[str] = []
_EXPECTING = [0]


def _refuse(what: str) -> None:
    if not _EXPECTING[0]:
        _UNEXPECTED.append(what)
    raise NetworkBlocked(f"network access is blocked in tests: {what}")


def _is_unix(sock: socket.socket) -> bool:
    return getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX


def _connect(original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: socket.socket, address: Any, *args: Any) -> Any:
        if _is_unix(self):
            return original(self, address, *args)
        _refuse(f"{original.__name__} {self.family.name}")
        return None  # pragma: no cover - _refuse always raises

    guarded.network_guard = True  # type: ignore[attr-defined]
    return guarded


def _function(name: str) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        _refuse(name)

    guarded.network_guard = True  # type: ignore[attr-defined]
    return guarded


def install() -> None:
    if installed():
        return
    _ORIGINAL.update(
        connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex,
        create_connection=socket.create_connection,
        getaddrinfo=socket.getaddrinfo,
        gethostbyname=socket.gethostbyname,
    )
    socket.socket.connect = _connect(_ORIGINAL["connect"])  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect(_ORIGINAL["connect_ex"])  # type: ignore[method-assign]
    socket.create_connection = _function("create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = _function("getaddrinfo")  # type: ignore[assignment]
    socket.gethostbyname = _function("gethostbyname")  # type: ignore[assignment]


def uninstall() -> None:
    if not installed():
        return
    socket.socket.connect = _ORIGINAL["connect"]  # type: ignore[method-assign]
    socket.socket.connect_ex = _ORIGINAL["connect_ex"]  # type: ignore[method-assign]
    socket.create_connection = _ORIGINAL["create_connection"]
    socket.getaddrinfo = _ORIGINAL["getaddrinfo"]
    socket.gethostbyname = _ORIGINAL["gethostbyname"]
    _ORIGINAL.clear()


def installed() -> bool:
    return all(
        getattr(target, "network_guard", False)
        for target in (
            socket.socket.connect,
            socket.socket.connect_ex,
            socket.create_connection,
            socket.getaddrinfo,
            socket.gethostbyname,
        )
    )


def unexpected() -> list[str]:
    """Refused attempts that no test expected (each fails the run)."""
    return list(_UNEXPECTED)


@contextmanager
def expected_block() -> Iterator[None]:
    """For tests of the guard itself: attempts inside are expected, not counted."""
    _EXPECTING[0] += 1
    try:
        yield
    finally:
        _EXPECTING[0] -= 1
