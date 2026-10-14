"""Draft validator (SPEC §8.2; docs/DECISIONS.md D-022). Pure.

``validate_draft`` finds every invoice-like token, AED amount and date in a draft and reports
the ones the fact block does not contain, plus forbidden phrases. Detection is a superset of
the spec patterns so that nothing slips through by formatting: an amount that cannot be read,
or a date that does not exist, is reported as unknown.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

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
    "validate_draft",
]

UNKNOWN_INVOICE = "UNKNOWN_INVOICE"
UNKNOWN_AMOUNT = "UNKNOWN_AMOUNT"
UNKNOWN_DATE = "UNKNOWN_DATE"
FORBIDDEN_PHRASE = "FORBIDDEN_PHRASE"
BODY_TOO_LONG = "BODY_TOO_LONG"  # §8.2 step 4 gives no code (D-022)

DEFAULT_INVOICE_REGEX = r"\b[A-Z]{2,6}-\d{2,}\b"

# §8.2 reads amounts with AED\s?([\d,]+(?:\.\d{1,2})?); capturing every digit, comma and point
# that follows means "AED 50,000.005" is read whole (and rejected), not as 50,000.00.
_AMOUNT = re.compile(r"\bAED\s?([0-9][0-9,.]*[0-9]|[0-9])", re.IGNORECASE)
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
_ISO_DATE = re.compile(r"\b([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})\b")
_DAY_MONTH_YEAR = re.compile(
    rf"\b([0-9]{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\.?,?\s+([0-9]{{4}})\b", re.I
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b({_MONTH})\.?\s+([0-9]{{1,2}})(?:st|nd|rd|th)?,?\s+([0-9]{{4}})\b", re.I
)
_NUMERIC_DATE = re.compile(r"\b([0-9]{1,2})[/.]([0-9]{1,2})[/.]([0-9]{4})\b")  # day first (UAE)

# §8.2 list, matched as whole words with their common inflections.
_FORBIDDEN = re.compile(
    r"\b(?:legal\s+action|lawsuits?|su(?:e|es|ed|ing)|courts?|police|criminal|"
    r"arrest(?:s|ed|ing)?|blacklist(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)


def _date(year: str, month: int | str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _mentioned_dates(text: str) -> list[date | None]:
    found: list[date | None] = []
    found += [_date(y, m, d) for y, m, d in _ISO_DATE.findall(text)]
    found += [_date(y, _MONTHS[m.lower()], d) for d, m, y in _DAY_MONTH_YEAR.findall(text)]
    found += [_date(y, _MONTHS[m.lower()], d) for m, d, y in _MONTH_DAY_YEAR.findall(text)]
    found += [_date(y, m, d) for d, m, y in _NUMERIC_DATE.findall(text)]
    return found


def _amount(raw: str) -> int | None:
    if not _AMOUNT_FORMAT.fullmatch(raw):
        return None
    try:
        return to_fils(Decimal(raw.replace(",", "")))
    except (InvalidOperation, ValueError):  # pragma: no cover - the format already checked it
        return None


def validate_draft(
    text: str, facts: FactBlock, invoice_regex: str = DEFAULT_INVOICE_REGEX
) -> tuple[str, ...]:
    """Violations of ``text`` against ``facts``, distinct and sorted."""
    violations: set[str] = set()

    known_invoices = {i.invoice_number for i in facts.invoices}
    tokens = (m.group(0) for m in re.finditer(invoice_regex, text))  # whole match, not groups
    if any(token not in known_invoices for token in tokens):
        violations.add(UNKNOWN_INVOICE)

    known_amounts = {i.open_amount for i in facts.invoices} | {i.amount for i in facts.invoices}
    if facts.amount_difference is not None:
        known_amounts.add(facts.amount_difference)
    if any(_amount(raw) not in known_amounts for raw in _AMOUNT.findall(text)):
        violations.add(UNKNOWN_AMOUNT)

    known_dates = {i.due_date for i in facts.invoices} | {facts.as_of}
    if any(d not in known_dates for d in _mentioned_dates(text)):
        violations.add(UNKNOWN_DATE)

    if _FORBIDDEN.search(text):
        violations.add(FORBIDDEN_PHRASE)
    return tuple(sorted(violations))


def check_draft(
    subject: str,
    body: str,
    facts: FactBlock,
    *,
    max_words: int,
    invoice_regex: str = DEFAULT_INVOICE_REGEX,
) -> tuple[str, ...]:
    """``validate_draft`` over subject and body, plus the body word limit (§8.2 step 4)."""
    violations = set(validate_draft(f"{subject}\n{body}", facts, invoice_regex))
    if len(body.split()) > max_words:
        violations.add(BODY_TOO_LONG)
    return tuple(sorted(violations))
