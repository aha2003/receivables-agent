"""Passwords, login rate limiting and session validity (SPEC §3.3)."""

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from receivables.adapters.clock import Clock

__all__ = [
    "SESSION_TTL",
    "LoginRateLimiter",
    "SessionUser",
    "hash_password",
    "session_payload",
    "session_user",
    "verify_password",
]

SESSION_TTL = timedelta(minutes=480)  # §3.3 [A]; becomes configurable with config.yaml (S1.13)

_HASHER = PasswordHasher()  # Argon2id with the library's recommended parameters
# Unknown usernames are checked against this hash so they cost the same time as a wrong password.
_DUMMY_HASH = _HASHER.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _HASHER.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


class LoginRateLimiter:
    """At most ``limit`` failed logins per ``window`` per (client, username); clock-driven."""

    def __init__(self, clock: Clock, limit: int = 5, window: timedelta = timedelta(minutes=1)):
        self._clock = clock
        self._limit = limit
        self._window = window
        self._failures: dict[tuple[str, str], deque[datetime]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str]) -> deque[datetime]:
        cutoff = self._clock.now() - self._window
        failures = self._failures.setdefault(key, deque())
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self._limit

    def record_failure(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._recent(key).append(self._clock.now())

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)


@dataclass(frozen=True, slots=True)
class SessionUser:
    user_id: int
    username: str
    role: str


def session_payload(user: SessionUser, clock: Clock) -> dict[str, Any]:
    """What the signed cookie carries: identity and issue time, never secrets."""
    return {
        "uid": user.user_id,
        "username": user.username,
        "role": user.role,
        "iat": clock.now().isoformat(),
    }


def session_user(session: Mapping[str, Any], clock: Clock) -> SessionUser | None:
    """The session's user if it is well formed and younger than ``SESSION_TTL``."""
    try:
        issued = datetime.fromisoformat(str(session["iat"]))
        user = SessionUser(int(session["uid"]), str(session["username"]), str(session["role"]))
    except (KeyError, TypeError, ValueError):
        return None
    if issued.tzinfo is None:
        return None
    age = clock.now() - issued
    if age < timedelta(0) or age > SESSION_TTL:
        return None
    return user
