"""Network guard for the whole test suite (SPEC §3.5 "Tests never call the real API"; D-029).

Installed by ``tests/conftest.py`` at session start, before any test module is imported, and
removed at session end. Every TCP/UDP connection or datagram and every forward or reverse DNS
lookup raises ``NetworkBlocked``; Unix-domain sockets stay allowed (local IPC, not the network).
Each refused attempt is counted, so an attempt that some library catches and swallows still
fails the run, unless a test deliberately expects it inside ``expected_block()``.
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


# Socket methods that reach a remote address (sendto/sendmsg: UDP needs no connect first).
_METHODS = ("connect", "connect_ex", "sendto", "sendmsg")
# Module functions that connect or resolve names (forward and reverse DNS).
_FUNCTIONS = (
    "create_connection",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)


def _method(original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: socket.socket, *args: Any) -> Any:
        if _is_unix(self):
            return original(self, *args)
        _refuse(f"{original.__name__} {self.family.name}")
        return None  # pragma: no cover - _refuse always raises

    guarded.network_guard = True  # type: ignore[attr-defined]
    return guarded


def _function(name: str) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        _refuse(name)

    guarded.network_guard = True  # type: ignore[attr-defined]
    return guarded


def _targets() -> list[tuple[Any, str]]:
    targets: list[tuple[Any, str]] = [
        (socket.socket, name) for name in _METHODS if hasattr(socket.socket, name)
    ]
    return targets + [(socket, name) for name in _FUNCTIONS]


def install() -> None:
    if installed():
        return
    for owner, name in _targets():
        original = getattr(owner, name)
        _ORIGINAL[name] = original
        guarded = _method(original) if owner is socket.socket else _function(name)
        setattr(owner, name, guarded)


def uninstall() -> None:
    if not installed():
        return
    for owner, name in _targets():
        setattr(owner, name, _ORIGINAL[name])
    _ORIGINAL.clear()


def installed() -> bool:
    return all(getattr(getattr(owner, name), "network_guard", False) for owner, name in _targets())


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
