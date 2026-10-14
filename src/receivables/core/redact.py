"""Redaction and pseudonymisation before any text is sent to the LLM (SPEC §7.5; D-030).

``redact`` applies the §7.5 rules in their order: 15-digit numbers become ``[TRN]``, UAE IBANs
``[IBAN]``, email addresses ``[EMAIL]`` and UAE phone numbers ``[PHONE]``. ``pseudonymize``
replaces every case-insensitive occurrence of each customer name with ``[CUSTOMER]``.
``prepare_for_llm`` runs both, each behind its config switch.

Digits match Arabic-Indic forms as well as ASCII. Beyond the literal §7.5 wording, an IBAN may be
printed in groups or in lower case, and a phone may carry "(0)" after the country code (D-030).
These steps reduce exposure; they do not remove personal names, other bank formats or other
confidential content (§7.5 "Limits").
"""

import re
from collections.abc import Iterable

__all__ = ["prepare_for_llm", "pseudonymize", "redact"]

_ZERO = "[0\u0660\u06f0]"  # ASCII, Arabic-Indic and Extended Arabic-Indic zero
_UAE = "(?:971|\u0669\u0667\u0661|\u06f9\u06f7\u06f1)"  # the UAE country code, 971

# One gap between digits: a space (also no-break or thin), a tab or a hyphen.
_GAP = "[ \t\u00a0\u2007\u2009\u202f-]"
_TOKENS = ("TRN", "IBAN", "EMAIL", "PHONE", "CUSTOMER")

_TRN = re.compile(r"(?<!\d)\d{15}(?!\d)")
# AE and 21 digits, with any whitespace or hyphens between digits (printed groups, a wrapped
# line; D-030). Gaps and digits are disjoint, so matching stays linear.
_IBAN = re.compile(r"[Aa][Ee](?:[\s-]*\d){21}(?!\d)")
# The local part may already hold a masked TRN or IBAN, so the domain is not left behind.
_EMAIL = re.compile(r"(?:[\w.!#$%&'*+/=?^`{|}~-]|\[TRN\]|\[IBAN\])+@[\w-]+(?:\.[\w-]+)+")
# A phone number starts a number: not after a digit, a Latin letter, "+", "_" or a hyphen (a
# reference such as INV-0001234567); an Arabic letter may come first (و is written joined). Then
# an optional "(0)" after the country code, and 8 (landline) or 9 (mobile) digits, each
# optionally after one gap. Lists such as "050 123 4567, 050 765 4321" are all masked.
_PHONE = re.compile(
    rf"(?<![\dA-Za-z_+-])"
    rf"(?:(?:\+{_GAP}?{_UAE}|{_ZERO}{_ZERO}{_GAP}?{_UAE})(?:{_GAP}?\({_ZERO}\))?|{_ZERO})"
    rf"(?:{_GAP}?\d){{8,9}}(?!\d)"
)

_RULES = ((_TRN, "[TRN]"), (_IBAN, "[IBAN]"), (_EMAIL, "[EMAIL]"), (_PHONE, "[PHONE]"))


def redact(text: str) -> str:
    """Replace TRNs, UAE IBANs, email addresses and UAE phone numbers, in §7.5 order."""
    for pattern, token in _RULES:
        text = pattern.sub(token, text)
    return text


def pseudonymize(text: str, customer_names: Iterable[str]) -> str:
    """Replace every case-insensitive occurrence of each customer name with ``[CUSTOMER]``.

    Longer names are tried first, so "Beta Contracting LLC" is not cut short by "Beta". Names
    are matched as literal text; blank names are ignored.
    """
    names = sorted({n.strip() for n in customer_names if n.strip()}, key=lambda n: (-len(n), n))
    if not names:
        return text
    # Tokens from redaction are kept whole, so a customer called "Iban" cannot hide an [IBAN].
    tokens = "|".join(re.escape(f"[{t}]") for t in _TOKENS)
    names_pattern = "|".join(re.escape(n) for n in names)
    pattern = re.compile(rf"(?P<token>(?-i:{tokens}))|{names_pattern}", re.IGNORECASE)
    return pattern.sub(lambda m: m.group() if m.group("token") else "[CUSTOMER]", text)


def prepare_for_llm(
    text: str,
    *,
    redact_before_send: bool,
    pseudonymize_customer_names: bool,
    customer_names: Iterable[str],
) -> str:
    """Text as it may be sent to the LLM: redaction first, then customer names (§7.5)."""
    if redact_before_send:
        text = redact(text)
    if pseudonymize_customer_names:
        text = pseudonymize(text, customer_names)
    return text
