"""Deterministic verification of an LLM extraction (SPEC §7.4; docs/DECISIONS.md D-033).

``verify_extraction`` checks that what the model extracted is faithful to the message and
matches the local ledger where it can. It never decides whether the customer is right.

- Invoice references match the stable ``invoice_number`` (ignoring case and outer spaces),
  else a ``display_invoice_number``; no match is ``UNMATCHED_INVOICE``, and a display number
  shared by several invoices is ``UNMATCHED_INVOICE`` with ``ambiguous`` (no match is chosen).
- ``evidence_quote`` must be a substring of the message, or of the redacted text the model saw,
  after whitespace runs become one space; otherwise ``E_EVIDENCE_NOT_VERBATIM``.
- Every reference, date and amount must appear in the message after deterministic
  normalisation (NFKC, case-folding, Arabic-Indic digits and separators as ASCII, whitespace);
  amounts compare as ``Decimal``; a currency other than AED must appear too. Otherwise
  ``UNVERIFIED_EXTRACTION`` naming the field.
- ``PO_AMOUNT_MISMATCH``: ``amount_difference = invoice amount - po_amount`` (fils), only for
  exactly one matched invoice and only when the PO role is verified: in the amount's sentence,
  the nearest earlier mention (a keyword or a reference) is a purchase order, not an invoice.
- ``ALREADY_PAID`` on an invoice still open gives ``LEDGER_STILL_OPEN`` with the open amount.
- A matched invoice of another known customer gives ``CUSTOMER_INVOICE_MISMATCH``.
- ``needs_review``: confidence below the threshold, ``UNCLEAR``, any flag, or a
  ``PAYMENT_PROMISED`` without a promised date. Confidence is used for routing only.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from receivables.core.ledger import LedgerRow
from receivables.core.money import parse_written_amount, to_fils

__all__ = [
    "AmountClaim",
    "DateClaim",
    "Extraction",
    "Flag",
    "Verification",
    "verify_extraction",
]

BASE_CURRENCY = "AED"


@dataclass(frozen=True, slots=True)
class AmountClaim:
    value: str
    currency: str
    role: str


@dataclass(frozen=True, slots=True)
class DateClaim:
    value: str
    role: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Extraction:
    """What the model extracted (§7.2), as plain values."""

    label: str
    confidence: float
    invoice_numbers: tuple[str, ...]
    po_numbers: tuple[str, ...]
    amounts: tuple[AmountClaim, ...]
    dates: tuple[DateClaim, ...]
    promised_payment_date: str | None
    evidence_quote: str


@dataclass(frozen=True, slots=True)
class Flag:
    code: str
    invoice_number: str | None = None
    amount: int | None = None  # fils
    field: str | None = None  # which extracted field could not be verified
    ambiguous: bool = False
    candidates: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The stored shape (D-027): only the keys that carry a value."""
        out: dict[str, Any] = {"code": self.code}
        for key in ("invoice_number", "amount", "field", "candidates"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.ambiguous:
            out["ambiguous"] = True
        return dict(sorted(out.items(), key=lambda kv: kv[0] != "code"))


@dataclass(frozen=True, slots=True)
class Verification:
    flags: tuple[Flag, ...]
    needs_review: bool
    amount_difference: int | None  # fils; ledger invoice amount minus the PO amount
    matched: tuple[str, ...] = field(default=())  # canonical invoice numbers, in order


# --- normalisation ------------------------------------------------------------------------------

_SEPARATORS = str.maketrans({"\u066c": ",", "\u066b": "."})  # Arabic thousands and decimal


def _digits(text: str) -> str:
    """Every decimal digit (Arabic-Indic and others) as ASCII."""
    return "".join(str(unicodedata.digit(ch)) if ch.isdecimal() else ch for ch in text)


def _normal(text: str) -> str:
    text = _digits(unicodedata.normalize("NFKC", text).translate(_SEPARATORS)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- amounts in the message ---------------------------------------------------------------------

# A number that is not part of a reference or a date: no Latin letter, digit, separator, slash
# or hyphen before it, and no digit, letter, slash or hyphen after it (so INV-004, INV004 and
# 2026-10-28 give no amounts; "AED47,000" fails safe as unverified).
_NUMBER = re.compile(
    r"(?<![A-Za-z_\d.,/-])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![A-Za-z_\d/-]|[.,]\d)"
)


def _amounts(text: str) -> list[tuple[Decimal, int]]:
    """Each number in normalised text with its position."""
    return [
        (Decimal(m.group(1).replace(",", "") + (m.group(2) or "")), m.start())
        for m in _NUMBER.finditer(text)
    ]


def _decimal(value: str) -> Decimal | None:
    """The model's amount as written in the message (``47,000``), as an exact ``Decimal``."""
    return parse_written_amount(value)  # the same rule as the response schema


# --- the PO role --------------------------------------------------------------------------------

_PO = re.compile(r"\bpurchase order|\bpo\b|أمر (?:ال)?شراء")
_INVOICE = re.compile(r"\binvoic\w*|\binv\b|\bbill\w*|\bcharg\w*|فاتورة")
_SENTENCE_END = re.compile(r"[!?؟\n]|\.(?=\s|$)")
_PO_ABBREVIATION = re.compile(r"\bp\.\s?o\.")


def _role_text(text: str) -> str:
    """``p.o.`` as ``po`` padded to the same length, so its dots end no sentence."""
    return _PO_ABBREVIATION.sub(lambda m: "po".ljust(len(m.group())), text)


def _is_po_context(text: str, position: int) -> bool:
    """In this sentence, is the nearest mention before ``position`` a purchase order, with no
    other amount between them (that amount would be the one the mention is about)?"""
    ends = [m.end() for m in _SENTENCE_END.finditer(text, 0, position)]
    before = text[(ends[-1] if ends else 0) : position]
    mentions = [(m.end(), "po") for m in _PO.finditer(before)]
    mentions += [(m.end(), "invoice") for m in _INVOICE.finditer(before)]
    if not mentions:
        return False
    nearest, kind = max(mentions)
    return kind == "po" and not _NUMBER.search(before, nearest)


# --- verification -------------------------------------------------------------------------------


def verify_extraction(
    extraction: Extraction,
    *,
    message: str,
    sent_text: str | None,
    ledger: Iterable[LedgerRow],
    message_customer_id: str | None,
    review_threshold: Fraction,
) -> Verification:
    rows = list(ledger)
    by_number = {r.invoice_number.strip().upper(): r for r in rows}
    by_display: dict[str, list[LedgerRow]] = {}
    for r in rows:
        if r.display_invoice_number:
            by_display.setdefault(r.display_invoice_number.strip().upper(), []).append(r)

    text = _normal(message)
    numbers = _amounts(text)
    flags: list[Flag] = []

    def unverified(name: str) -> None:
        flags.append(Flag("UNVERIFIED_EXTRACTION", field=name))

    def quoted(value: str) -> bool:
        """``value`` appears as a whole token: INV-001 is not found inside INV-0012."""
        needle = _normal(value)
        if not needle:
            return False
        start = r"(?<![\w-])" if re.match(r"\w", needle) else ""
        end = r"(?![\w]|-\w)" if re.match(r"\w", needle[-1]) else ""
        return re.search(start + re.escape(needle) + end, text) is not None

    # Invoice references.
    matched: list[LedgerRow] = []
    for reference in extraction.invoice_numbers:
        key = reference.strip().upper()
        if not quoted(reference):
            unverified("invoice_numbers")
        if key in by_number:
            matched.append(by_number[key])
        elif len(by_display.get(key, ())) == 1:
            matched.append(by_display[key][0])
        elif key in by_display:
            flags.append(
                Flag(
                    "UNMATCHED_INVOICE",
                    invoice_number=reference.strip(),
                    ambiguous=True,
                    candidates=len(by_display[key]),
                )
            )
        else:
            flags.append(Flag("UNMATCHED_INVOICE", invoice_number=reference.strip()))
    matched = list({r.invoice_number: r for r in matched}.values())

    # Evidence.
    quote = _spaces(extraction.evidence_quote)
    sources = [_spaces(message)] + ([_spaces(sent_text)] if sent_text is not None else [])
    if not quote or not any(quote in source for source in sources):
        flags.append(Flag("E_EVIDENCE_NOT_VERBATIM"))

    # Every other value needs its supporting quote.
    for po in extraction.po_numbers:
        if not quoted(po):
            unverified("po_numbers")
    for claim in extraction.dates:
        if not quoted(claim.value):
            unverified("dates_mentioned")
    if extraction.promised_payment_date is not None and not quoted(
        extraction.promised_payment_date
    ):
        unverified("promised_payment_date")
    positions: dict[AmountClaim, list[int]] = {}
    for claim in extraction.amounts:
        value = _decimal(claim.value)
        positions[claim] = [at for number, at in numbers if value is not None and number == value]
        currency_ok = claim.currency.upper() == BASE_CURRENCY or re.search(
            rf"\b{re.escape(claim.currency.casefold())}\b", text
        )
        if not positions[claim] or not currency_ok:
            unverified("amounts_mentioned")

    # The PO amount difference (code-computed, role verified first).
    difference: int | None = None
    po_claims = [c for c in extraction.amounts if c.role == "po_amount"]
    if extraction.label == "PO_AMOUNT_MISMATCH" and po_claims:
        role_text = _role_text(text)
        verified: set[Decimal] = set()
        all_verified = True
        for claim in po_claims:
            value = _decimal(claim.value)
            if value is None or not any(_is_po_context(role_text, at) for at in positions[claim]):
                unverified("po_amount")
                all_verified = False
            else:
                verified.add(value)
        if len(verified) > 1:  # two different PO amounts: never choose one
            unverified("po_amount")
        elif all_verified and len(matched) == 1:
            (po_amount,) = verified
            difference = matched[0].amount - to_fils(po_amount.quantize(Decimal("0.01")))

    # The ledger.
    if extraction.label == "ALREADY_PAID":
        flags += [
            Flag("LEDGER_STILL_OPEN", invoice_number=r.invoice_number, amount=r.open_amount)
            for r in matched
            if r.open_amount > 0
        ]
    if message_customer_id is not None:
        flags += [
            Flag("CUSTOMER_INVOICE_MISMATCH", invoice_number=r.invoice_number)
            for r in matched
            if r.customer_id != message_customer_id
        ]

    unique = tuple(dict.fromkeys(flags))
    confidence = Fraction(repr(float(extraction.confidence)))  # 0.6 is 3/5, not a binary float
    needs_review = (
        confidence < review_threshold
        or extraction.label == "UNCLEAR"
        or bool(unique)
        or (extraction.label == "PAYMENT_PROMISED" and extraction.promised_payment_date is None)
        # D-033: a PO mismatch whose difference could not be computed is never auto-accepted.
        or (extraction.label == "PO_AMOUNT_MISMATCH" and bool(po_claims) and difference is None)
    )
    return Verification(unique, needs_review, difference, tuple(r.invoice_number for r in matched))
