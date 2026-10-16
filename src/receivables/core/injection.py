"""A deterministic warning for instruction-injection attempts in inbound messages (SPEC §18.9;
T-INJ-01; docs/DECISIONS.md D-039).

``injection_signals`` reads the original message text and names what looks like an attempt to
instruct the system rather than talk about an invoice:

- ``override``: "ignore previous instructions", "system prompt", "you are now", "developer
  mode" and the like, in English or Arabic;
- ``recipient``: a request to send, email, forward or export something *to* an email address
  that is not one of ``allowed_addresses`` (the customer's stored contact address);
- ``ledger``: a request to mark or set invoices or accounts as paid, settled or closed.

This is a routing aid only: a signal sends the message to review (``needs_review``). It is not
the defence and nothing relies on it. The defences are structural: the message is quoted data
to the classifier only, plans may use only the 11 registry tools with schema-checked arguments
(no recipient, amount or path), drafts are validated, and the gateway sends only to the stored
address. Rewording can avoid these patterns; an honest customer can trigger them. Both only
change whether a person looks at the message.
"""

import re
import unicodedata
from collections.abc import Iterable

__all__ = ["SIGNALS", "injection_signals"]

SIGNALS = ("override", "recipient", "ledger")

# Every pattern is linear on untrusted text up to the import limit: quantifiers are bounded and
# possessive (``++``, ``{m,n}+``), so a crafted message cannot make matching backtrack.
_TATWEEL = "ـ"

_OVERRIDE = [
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b(?:\W{1,5}+\w{1,30}+){0,3}?\W{1,5}+"
        r"(?:previous|prior|above|earlier|preceding|all|any|your|the|system|these|those)"
        r"(?:\W{1,5}+\w{1,30}+){0,2}?\W{1,5}+"
        r"(?:instructions?|prompts?|rules?|directions?|guidelines?)\b"
    ),
    re.compile(r"\bsystem (?:prompt|message|instructions?)\b"),
    re.compile(r"\b(?:reveal|show|print|repeat|output) (?:me )?(?:your|the) prompt\b"),
    re.compile(r"\byou are now\b"),
    re.compile(r"\bnew instructions?\b"),
    re.compile(r"\b(?:developer|admin|god) mode\b"),
    re.compile(r"\b(?:jailbreak|prompt injection)\b"),
    # Arabic: "ignore (all) (your/the) instructions", "(your/the) previous instructions",
    # "you are now"; with or without the article and with a possessive suffix.
    re.compile(
        r"(?:تجاهل|تجاهلي|تجاهلوا|انس|انسي|اهمل|أهمل) (?:\S{1,30}+ ){0,3}?\S{0,4}?"
        r"(?:تعليمات|أوامر|اوامر)"
    ),
    re.compile(r"(?:تعليمات|أوامر|اوامر)\S{0,3}+ (?:ال)?سابق"),
    re.compile(r"(?:انت|أنت) (?:الان|الآن)"),
]

_SEND = re.compile(
    r"\b(?:send|e-?mail|forward|mail|export|share|transfer|copy|cc|bcc)\b"
    r"|(?:أرسل|ارسل|ارسلوا|أرسلوا|ابعث|إبعث)"
)
_TO = re.compile(r"\b(?:to|cc|bcc)\b|إلى|الى")  # a recipient follows the verb
_ADDRESS = re.compile(r"[\w.+-]{1,64}+@[\w-]{1,63}+(?:\.[\w-]{1,63}+){1,8}+")
_SEND_WINDOW = 120  # characters between the verb and the address, within one message

_LEDGER = re.compile(
    r"\b(?:mark|set|record|update|change|make) (?:\S{1,30}+ ){0,6}?"
    r"(?:invoices?|accounts?|bills?|balances?)\b(?: \S{1,30}+){0,6}? (?:as|to) "
    r"(?:paid|settled|closed|cleared|zero)\b"
)


def _normal(text: str) -> str:
    """NFKC, case-folded, without combining marks (Arabic harakat, the dot of Turkish İ) or
    format characters (zero-width and bidirectional controls), tatweel removed, and every
    whitespace run as one space."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(
        c for c in folded if c != _TATWEEL and unicodedata.category(c) not in ("Mn", "Cf")
    )
    return re.sub(r"\s++", " ", kept)


def _asks_to_send_to(normal: str, address: re.Match[str]) -> bool:
    before = normal[max(0, address.start() - _SEND_WINDOW) : address.start()]
    verbs = list(_SEND.finditer(before))
    return bool(verbs) and _TO.search(before, verbs[-1].start()) is not None


def injection_signals(text: str, *, allowed_addresses: Iterable[str] = ()) -> tuple[str, ...]:
    """The signals found in ``text`` (in ``SIGNALS`` order); empty when none."""
    normal = _normal(text)
    allowed = {a.strip().casefold() for a in allowed_addresses if a and a.strip()}
    found: set[str] = set()
    if any(p.search(normal) for p in _OVERRIDE):
        found.add("override")
    for address in _ADDRESS.finditer(normal):
        if address.group().rstrip(".").casefold() in allowed:
            continue
        if _asks_to_send_to(normal, address):
            found.add("recipient")
            break
    if _LEDGER.search(normal):
        found.add("ledger")
    return tuple(s for s in SIGNALS if s in found)
