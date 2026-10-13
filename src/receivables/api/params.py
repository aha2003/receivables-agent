"""Shared request parameters: ``as_of`` (§2: defaults to today) and the queue ``limit``."""

import re
from datetime import date

from fastapi import HTTPException, Request

__all__ = ["parse_as_of", "parse_limit"]

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_as_of(request: Request, raw: str | None) -> date:
    if raw is None:
        return request.app.state.clock.today()
    if not _ISO_DATE.fullmatch(raw):
        raise HTTPException(422, "as_of must be a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, "as_of must be a real calendar date") from None


def parse_limit(raw: str | None, maximum: int) -> int:
    if raw is None:
        return maximum
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise HTTPException(422, f"limit must be a whole number from 1 to {maximum}")
    return int(raw)
