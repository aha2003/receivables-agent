"""Plain-language text for codes shown in the UI (SPEC §10, §6.7, §7.4, §18.5; D-027).

§10: flags are shown in plain language, never hidden. A code without an entry here is shown
as the bare code, never dropped.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from receivables.core.money import from_fils, to_fils

__all__ = ["POLICY_TEXT", "STATUS_TEXT", "flag_text", "policy_text", "status_text"]

POLICY_TEXT = {
    "POLICY_KILL_SWITCH": "The kill switch is on: nothing is sent.",
    "POLICY_NOT_ALLOWLISTED": "This customer is not on the autopilot allowlist.",
    "POLICY_AMOUNT_LIMIT": "The open amount in the message is above the autopilot limit.",
    "POLICY_TONE_NOT_ALLOWED": "Only a friendly (tone 1) reminder may be sent automatically.",
    "POLICY_DISPUTE_ACTIVE": "An invoice in the message has an open dispute.",
    "POLICY_UNALLOCATED_CASH": "The customer has unallocated cash on account.",
    "POLICY_RECENT_CONTACT": "The customer was contacted less than 7 days ago.",
    "POLICY_OUTSIDE_HOURS": "Outside business hours (Monday to Friday, 09:00 to 17:00 Dubai).",
    "POLICY_DAILY_CAP": "Today's limit of automatic sends has been reached.",
    "POLICY_VALIDATION_FAILED": "The draft failed validation against the facts.",
    "POLICY_RECIPIENT_NOT_ALLOWED": "The recipient is not the customer's stored email address.",
}

STATUS_TEXT = {
    "executed": "done",
    "denied": "denied by policy",
    "converted_to_approval": "waiting for human approval",
    "failed": "failed",
    "skipped": "skipped (shadow mode)",
}


def policy_text(code: str) -> str:
    return POLICY_TEXT.get(code, code)


def status_text(status: str) -> str:
    return STATUS_TEXT.get(status, status)


def _aed(amount: Any) -> str | None:
    """A flag's amount (decimal string or integer fils) as ``AED 1,234.50``; None otherwise.

    Floats and booleans are refused (never float money); nothing here raises.
    """
    if isinstance(amount, bool) or not isinstance(amount, int | str):
        return None
    try:
        fils = amount if isinstance(amount, int) else to_fils(Decimal(amount))
        return f"AED {from_fils(fils):,}"
    except (InvalidOperation, TypeError, ValueError):
        return None


def flag_text(flag: Any) -> str:
    """A flag (a code string or ``{code, invoice_number?, amount?}``) in plain language."""
    data: Mapping[str, Any] = flag if isinstance(flag, Mapping) else {"code": flag}
    code = str(data.get("code", "?"))
    invoice = data.get("invoice_number")
    amount = _aed(data.get("amount")) if data.get("amount") is not None else None
    if code == "UNALLOCATED_CASH_ON_ACCOUNT" and amount:  # §6.7 wording
        return (
            f"This customer has {amount} of unallocated cash. "
            "Check whether it belongs to this invoice before chasing."
        )
    if code == "LEDGER_STILL_OPEN":  # §7.4 wording; the page passes today's ledger amount
        if data.get("settled"):
            return "Customer said this invoice was paid; the ledger now shows it settled."
        if amount:
            return (
                f"Customer says paid but the ledger shows {amount} open. Check the bank statement."
            )
        return (
            "Customer says paid but the ledger still shows this invoice open. "
            "Check the bank statement."
        )
    if code == "UNMATCHED_INVOICE":
        ref = f" {invoice}" if invoice else ""
        return (
            f"The message mentions invoice{ref}, which is not in the ledger. Check the reference."
        )
    if code == "CUSTOMER_INVOICE_MISMATCH":
        return "The message names an invoice that belongs to a different customer."
    if code == "E_EVIDENCE_NOT_VERBATIM":
        return "The quoted evidence does not appear word for word in the message. Review it."
    if code == "UNVERIFIED_EXTRACTION":
        return "A value read from the message could not be verified against its quote. Review it."
    return code
