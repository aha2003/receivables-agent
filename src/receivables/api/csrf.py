"""Per-session CSRF tokens (SPEC §3.3; docs/DECISIONS.md D-016).

Every request except GET, HEAD, OPTIONS and TRACE, including ``/login``, must carry the
session's token in the ``X-CSRF-Token`` header or a ``csrf_token`` form field; otherwise 403.
The check runs as an app-wide dependency so the parsed form stays available to the route.
"""

import secrets
from collections.abc import MutableMapping
from typing import Any

from fastapi import Request

__all__ = ["CSRF_SESSION_KEY", "CsrfError", "csrf_protect", "ensure_token", "new_token"]

CSRF_SESSION_KEY = "csrf"
# Everything except these is checked, so a route with an unusual method cannot skip CSRF.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class CsrfError(Exception):
    """Missing or invalid CSRF token (rendered as 403 ``csrf_failed``)."""


def new_token(session: MutableMapping[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def ensure_token(session: MutableMapping[str, Any]) -> str:
    token = session.get(CSRF_SESSION_KEY)
    return token if isinstance(token, str) and token else new_token(session)


async def csrf_protect(request: Request) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    expected = request.session.get(CSRF_SESSION_KEY)
    provided = request.headers.get("x-csrf-token")
    content_type = request.headers.get("content-type", "").strip().lower()  # case-insensitive
    if provided is None and content_type.startswith(_FORM_TYPES):
        value = (await request.form()).get("csrf_token")
        provided = value if isinstance(value, str) else None
    if not (
        isinstance(expected, str)
        and expected
        and provided
        and secrets.compare_digest(provided.encode(), expected.encode())
    ):
        raise CsrfError
