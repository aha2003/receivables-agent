"""Turning a date as written in a message into a calendar date (SPEC §7.2; D-034).

The model returns dates as it saw them ("20 October", "٢٠ أكتوبر", "20/10"); code resolves
them. A date without a year becomes the occurrence nearest to the message's ``sent_at`` date
(a tie goes to the future one). Day-first numeric dates are read as day/month, the UAE
convention. Anything else, or an impossible date, resolves to ``None``.
"""

import re
import unicodedata
from datetime import date

__all__ = ["resolve_message_date"]

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    # Arabic (Gulf usage), with and without hamza.
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10,
    "نوفمبر": 11, "ديسمبر": 12,
}  # fmt: skip
_MONTH = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAY_MONTH = re.compile(
    rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\.?,?(?:\s+(\d{{4}}))?"
)
_MONTH_DAY = re.compile(rf"({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?")
_NUMERIC = re.compile(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{4}))?")


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(str(unicodedata.digit(ch)) if ch.isdecimal() else ch for ch in text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nearest(month: int, day: int, around: date) -> date | None:
    """The occurrence nearest to ``around``; ``date`` itself refuses impossible days."""
    candidates = [d for y in (around.year - 1, around.year, around.year + 1)
                  if (d := _make(y, month, day)) is not None]  # fmt: skip
    if not candidates:
        return None
    return min(candidates, key=lambda d: (abs((d - around).days), d < around))


def resolve_message_date(value: str, sent_on: date) -> date | None:
    """The calendar date ``value`` names, read relative to the message date ``sent_on``."""
    text = _plain(value)
    if m := _ISO.fullmatch(text):
        return _make(int(m[1]), int(m[2]), int(m[3]))
    for pattern, day_first in ((_DAY_MONTH, True), (_MONTH_DAY, False)):
        if m := pattern.fullmatch(text):
            day, month = (m[1], m[2]) if day_first else (m[2], m[1])
            year = m[3]
            if year:
                return _make(int(year), _MONTHS[month], int(day))
            return _nearest(_MONTHS[month], int(day), sent_on)
    if m := _NUMERIC.fullmatch(text):
        day, month, year = int(m[1]), int(m[2]), m[3]
        return _make(int(year), month, day) if year else _nearest(month, day, sent_on)
    return None
