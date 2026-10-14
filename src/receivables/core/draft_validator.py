"""Draft validator (SPEC §8.2; docs/DECISIONS.md D-022). Pure.

``validate_draft`` finds every invoice-like token, AED amount and date in a draft and reports
the ones the fact block does not contain, plus forbidden phrases. Detection is a superset of
the spec patterns so formatting cannot hide a value: the text is first normalised (Unicode
compatibility forms, any decimal digit to ASCII, Arabic separators, invisible format
characters removed, whitespace collapsed); an amount that cannot be read, or a date that does
not exist, is reported as unknown.
"""

import re
import unicodedata
from datetime import date
from decimal import Decimal

from receivables.core.drafts import FactBlock
from receivables.core.money import to_fils

__all__ = [
    "BODY_TOO_LONG",
    "DEFAULT_INVOICE_REGEX",
    "FORBIDDEN_PHRASE",
    "UNKNOWN_AMOUNT",
    "UNKNOWN_DATE",
    "UNKNOWN_INVOICE",
    "check_draft",
    "mentions",
    "validate_draft",
]

UNKNOWN_INVOICE = "UNKNOWN_INVOICE"
UNKNOWN_AMOUNT = "UNKNOWN_AMOUNT"
UNKNOWN_DATE = "UNKNOWN_DATE"
FORBIDDEN_PHRASE = "FORBIDDEN_PHRASE"
BODY_TOO_LONG = "BODY_TOO_LONG"  # §8.2 step 4 gives no code (D-022)

DEFAULT_INVOICE_REGEX = r"\b[A-Z]{2,6}-\d{2,}\b"

# §8.2 reads amounts with AED\s?([\d,]+(?:\.\d{1,2})?). Here the currency may touch other
# letters, and every digit, comma and point that follows is captured, so "AED 50,000.005" or
# "AED ,51,000" is read whole (and rejected) rather than partly.
_AMOUNT = re.compile(r"AED\s?([0-9,][0-9,.]*[0-9]|[0-9])", re.IGNORECASE)
_AMOUNT_FORMAT = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?")

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_D = r"(?<![0-9])"  # not preceded by a digit
_E = r"(?![0-9])"  # not followed by a digit
_YEAR_FIRST = re.compile(rf"{_D}([0-9]{{4}})[-/.]([0-9]{{1,2}})[-/.]([0-9]{{1,2}}){_E}")
_DAY_FIRST = re.compile(rf"{_D}([0-9]{{1,2}})[-/.]([0-9]{{1,2}})[-/.]([0-9]{{4}}){_E}")  # UAE
_DAY_MONTH_YEAR = re.compile(
    rf"{_D}([0-9]{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?,?\s+([0-9]{{4}}){_E}",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b({_MONTH})\b\.?\s+([0-9]{{1,2}})(?:st|nd|rd|th)?,?\s+([0-9]{{4}}){_E}", re.IGNORECASE
)

# §8.2 list. A word boundary is required before each term (so "issue" and "courtesy" pass) and
# after "sue" and "court"; the others also match longer forms ("legal actions", "criminally",
# "policeman", "policing", "blacklisted", "courthouse").
_FORBIDDEN = re.compile(
    r"\b(?:legal\s+action|lawsuit|su(?:e|es|ed|ing)\b|court(?:s|house|houses|room|rooms)?\b|"
    r"polic(?:e|ing)|criminal|arrest|blacklist)",
    re.IGNORECASE,
)

# Arabic decimal separator, thousands separator and comma.
_ARABIC_SEPARATORS = str.maketrans({0x066B: ".", 0x066C: ",", 0x060C: ","})


def _normalise(text: str) -> str:
    """Compatibility forms, ASCII digits, no invisible characters, single spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_ARABIC_SEPARATORS)
    chars: list[str] = []
    for ch in text:
        if unicodedata.category(ch) == "Cf":
            continue  # zero-width and bidi characters
        digit = unicodedata.decimal(ch, None)
        chars.append(str(digit) if digit is not None and not ch.isascii() else ch)
    return re.sub(r"\s+", " ", "".join(chars))


def _date(year: str, month: int | str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _mentioned_dates(text: str) -> list[date | None]:
    found: list[date | None] = []
    found += [_date(y, m, d) for y, m, d in _YEAR_FIRST.findall(text)]
    found += [_date(y, m, d) for d, m, y in _DAY_FIRST.findall(text)]
    found += [_date(y, _MONTHS[m.lower()], d) for d, m, y in _DAY_MONTH_YEAR.findall(text)]
    found += [_date(y, _MONTHS[m.lower()], d) for m, d, y in _MONTH_DAY_YEAR.findall(text)]
    return found


def _amount(raw: str) -> int | None:
    if not _AMOUNT_FORMAT.fullmatch(raw):
        return None
    return to_fils(Decimal(raw.replace(",", "")))


def _scan(text: str, invoice_regex: str) -> tuple[list[str], list[int | None], list[date | None]]:
    invoices = [m.group(0) for m in re.finditer(invoice_regex, text)]  # whole match, not groups
    amounts = [_amount(raw) for raw in _AMOUNT.findall(text)]
    return invoices, amounts, _mentioned_dates(text)


def validate_draft(text: str, facts: FactBlock, invoice_regex: str) -> tuple[str, ...]:
    """Violations of ``text`` against ``facts``, distinct and sorted.

    ``invoice_regex`` is the customer's configured ``drafts.invoice_number_regex``.
    """
    text = _normalise(text)
    invoices, amounts, dates = _scan(text, invoice_regex)
    known_invoices = {i.invoice_number for i in facts.invoices}
    known_amounts = {i.open_amount for i in facts.invoices} | {i.amount for i in facts.invoices}
    if facts.amount_difference is not None:
        known_amounts.add(facts.amount_difference)
    known_dates = {i.due_date for i in facts.invoices} | {facts.as_of}

    violations: set[str] = set()
    if any(token not in known_invoices for token in invoices):
        violations.add(UNKNOWN_INVOICE)
    if any(amount not in known_amounts for amount in amounts):
        violations.add(UNKNOWN_AMOUNT)
    if any(d not in known_dates for d in dates):
        violations.add(UNKNOWN_DATE)
    if _FORBIDDEN.search(text):
        violations.add(FORBIDDEN_PHRASE)
    return tuple(sorted(violations))


def mentions(text: str, invoice_regex: str) -> tuple[str, ...]:
    """Codes for any invoice, amount, date or forbidden phrase in ``text`` at all.

    For fixed text that must never mention facts, such as the configured sender name.
    """
    text = _normalise(text)
    invoices, amounts, dates = _scan(text, invoice_regex)
    found = {
        code
        for code, present in (
            (UNKNOWN_INVOICE, invoices),
            (UNKNOWN_AMOUNT, amounts),
            (UNKNOWN_DATE, dates),
            (FORBIDDEN_PHRASE, _FORBIDDEN.search(text)),
        )
        if present
    }
    return tuple(sorted(found))


def check_draft(
    subject: str, body: str, facts: FactBlock, *, max_words: int, invoice_regex: str
) -> tuple[str, ...]:
    """``validate_draft`` over subject and body, plus the body word limit (§8.2 step 4)."""
    violations = set(validate_draft(f"{subject}\n{body}", facts, invoice_regex))
    if len(body.split()) > max_words:
        violations.add(BODY_TOO_LONG)
    return tuple(sorted(violations))
