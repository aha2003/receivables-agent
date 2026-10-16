"""A deterministic warning for instruction-injection attempts in inbound messages (SPEC §18.9;
T-INJ-01; docs/DECISIONS.md D-039).

``injection_signals`` reads the original message text and names what looks like an attempt to
instruct the system rather than talk about an invoice:

- ``override``: "ignore previous instructions", "system prompt", "you are now", "developer
  mode" and the like, in English or Arabic;
- ``recipient``: a request to send, email, forward or export something to an email address
  that is not one of ``allowed_addresses`` (the customer's stored contact address);
- ``ledger``: a request to mark or set all invoices or accounts as paid, settled or closed.

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

# Zero-width and bidirectional controls are removed so they cannot split a phrase.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)
_ARABIC_MARKS = re.compile("[\u064b-\u065f\u0670\u0640]")  # harakat and tatweel

_OVERRIDE = [
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b(?:\W+\w+){0,3}?\W+"
        r"(?:previous|prior|above|earlier|preceding|all|any|your|the|system|these|those)"
        r"(?:\W+\w+){0,2}?\W+(?:instructions?|prompts?|rules?|directions?|guidelines?|orders?)\b"
    ),
    re.compile(r"\bsystem\s+(?:prompt|message|instructions?)\b"),
    re.compile(r"\b(?:reveal|show|print|repeat|output)\s+(?:me\s+)?(?:your|the)\s+prompt\b"),
    re.compile(r"\byou\s+are\s+now\b"),
    re.compile(r"\bnew\s+instructions?\b"),
    re.compile(r"\b(?:developer|admin|god)\s+mode\b"),
    re.compile(r"\b(?:jailbreak|prompt\s+injection)\b"),
    # Arabic: "ignore the (previous) instructions", "previous instructions", "you are now".
    re.compile(
        r"(?:تجاهل|تجاهلي|تجاهلوا|انس|اهمل|أهمل)\s+(?:\S+\s+){0,2}?(?:التعليمات|الأوامر|الاوامر)"
    ),
    re.compile(r"(?:التعليمات|الأوامر|الاوامر)\s+السابقة"),
    re.compile(r"(?:انت|أنت)\s+(?:الان|الآن)"),
]

_SEND = re.compile(
    r"\b(?:send|e-?mail|forward|mail|export|share|transfer|copy|cc|bcc)\b"
    r"|(?:أرسل|ارسل|ارسلوا|أرسلوا|حول|حوّل|ابعث|إبعث)"
)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SEND_WINDOW = 120  # characters between the verb and the address, within one message

_LEDGER = re.compile(
    r"\b(?:mark|set|record|update|change|make)\s+(?:all|every|each)\s+(?:of\s+)?"
    r"(?:the\s+|my\s+|our\s+)?(?:invoices?|accounts?|bills?|balances?)\s+(?:as\s+|to\s+)?"
    r"(?:paid|settled|closed|cleared|zero)\b"
)


def _normal(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).casefold()
    return re.sub(r"\s+", " ", _ARABIC_MARKS.sub("", folded))


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
        before = normal[max(0, address.start() - _SEND_WINDOW) : address.start()]
        if _SEND.search(before):
            found.add("recipient")
            break
    if _LEDGER.search(normal):
        found.add("ledger")
    return tuple(s for s in SIGNALS if s in found)
