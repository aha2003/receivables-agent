"""Redaction and pseudonymisation before any text is sent to the LLM (SPEC §7.5; D-030).

``redact`` applies the §7.5 rules in their order: 15-digit numbers become ``[TRN]``, UAE IBANs
``[IBAN]``, email addresses ``[EMAIL]`` and UAE phone numbers ``[PHONE]``. ``pseudonymize``
replaces every case-insensitive occurrence of each customer name with ``[CUSTOMER]``.
``prepare_for_llm`` runs both, each behind its config switch.

Digits match Arabic-Indic forms as well as ASCII. Beyond the literal §7.5 wording, an IBAN may be
printed in groups or in lower case (D-030). These steps reduce exposure; they do not remove
personal names, other bank formats or other confidential content (§7.5 "Limits").
"""

import re
from collections.abc import Iterable

__all__ = ["prepare_for_llm", "pseudonymize", "redact"]

_ZERO = "[0\u0660\u06f0]"  # ASCII, Arabic-Indic and Extended Arabic-Indic zero
_UAE = "(?:971|\u0669\u0667\u0661|\u06f9\u06f7\u06f1)"  # the UAE country code, 971

_TRN = re.compile(r"(?<!\d)\d{15}(?!\d)")
_IBAN = re.compile(r"(?<![^\W_])[Aa][Ee](?: ?\d){21}(?!\d)")
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# A phone number starts a number: not inside a word or a longer number, and not after a hyphen
# (a reference such as INV-0001234567). Then 8 (landline) or 9 (mobile) digits, optionally split
# by single spaces or hyphens. Lists such as "050 123 4567, 050 765 4321" or ".../04 123 4567"
# are all masked.
_PHONE = re.compile(
    rf"(?<![\w+-])(?:\+{_UAE}|{_ZERO}{_ZERO}{_UAE}|{_ZERO})(?:[ -]?\d){{8,9}}(?!\d)"
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
    pattern = re.compile("|".join(re.escape(n) for n in names), re.IGNORECASE)
    return pattern.sub("[CUSTOMER]", text)


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
