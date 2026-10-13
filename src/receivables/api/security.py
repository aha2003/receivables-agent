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

SESSION_TTL = timedelta(minutes=480)  # §3.3 [A] default; config.yaml app.session_ttl_minutes

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
    """At most ``limit`` login attempts that end in failure per ``window`` per (client, username).

    ``acquire`` reserves an attempt atomically before the (slow) password check, so concurrent
    requests cannot all pass the check; a successful login calls ``clear``. Expired keys are
    purged on every call and the number of keys is capped (fail closed), so memory stays
    bounded. Time comes from the injected clock.
    """

    def __init__(
        self,
        clock: Clock,
        limit: int = 5,
        window: timedelta = timedelta(minutes=1),
        max_keys: int = 10_000,
    ) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        self._attempts: dict[tuple[str, str], deque[datetime]] = {}
        self._lockouts: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def _purge(self, now: datetime) -> None:
        cutoff = now - self._window
        for key in list(self._attempts):
            attempts = self._attempts[key]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                del self._attempts[key]
        for key, started in list(self._lockouts.items()):
            if started <= cutoff:
                del self._lockouts[key]

    def acquire(self, key: tuple[str, str]) -> bool:
        """Reserve one attempt; False if the key is limited or the table is full."""
        with self._lock:
            now = self._clock.now()
            self._purge(now)
            attempts = self._attempts.get(key)
            if attempts is None:
                if len(self._attempts) >= self._max_keys:
                    return False
                attempts = self._attempts[key] = deque()
            if len(attempts) >= self._limit:
                return False
            attempts.append(now)
            return True

    def lockout_started(self, key: tuple[str, str]) -> bool:
        """True once per window for a limited key (so a lockout is audited once, not per try)."""
        with self._lock:
            now = self._clock.now()
            self._purge(now)
            if key in self._lockouts:
                return False
            self._lockouts[key] = now
            return True

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._lockouts.pop(key, None)

    def key_count(self) -> int:
        with self._lock:
            return len(self._attempts)


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


def session_user(
    session: Mapping[str, Any], clock: Clock, ttl: timedelta = SESSION_TTL
) -> SessionUser | None:
    """The session's user if it is well formed and younger than ``ttl``."""
    try:
        issued = datetime.fromisoformat(str(session["iat"]))
        user = SessionUser(int(session["uid"]), str(session["username"]), str(session["role"]))
    except (KeyError, TypeError, ValueError):
        return None
    if issued.tzinfo is None:
        return None
    age = clock.now() - issued
    if age < timedelta(0) or age > ttl:
        return None
    return user
