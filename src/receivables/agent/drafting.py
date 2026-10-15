"""Draft generation: the LLM first, the deterministic template as the fallback (SPEC §8.2).

``generate_draft`` asks the drafter for a subject and body from the fact block (§8.1), checks
each attempt with ``check_draft`` (§8.2 violations, the 140-word limit and the §18.9 contact
rule) and regenerates up to ``max_regenerations`` times. If every attempt violates, or the
drafter is unavailable, it uses the §8.4 template and records ``generator = template``.

The drafter sees the fact block behind redaction, so the customer name arrives as
``[CUSTOMER]``; code puts the real name back before the final check (D-031, D-036). A draft is
checked as it will be saved. The template must pass the same check; if it ever does not, that
is a bug and this raises instead of saving a violating draft. No I/O here: the drafter is a
port the caller supplies (``None`` means the template only).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from receivables.core.draft_validator import check_draft
from receivables.core.drafts import FactBlock, render_template

__all__ = [
    "INVALID_OUTPUT",
    "UNAVAILABLE",
    "DraftInvalid",
    "DraftOutcome",
    "DraftUnavailable",
    "DraftWriter",
    "TemplateViolation",
    "generate_draft",
]

CUSTOMER_PLACEHOLDER = "[CUSTOMER]"
INVALID_OUTPUT = "INVALID_OUTPUT"  # an attempt whose output was not valid JSON for the schema
UNAVAILABLE = "UNAVAILABLE"  # the drafter could not be reached; no further attempts

# The fact block as JSON in, (subject, body) out.
DraftWriter = Callable[[Mapping[str, Any]], tuple[str, str]]


class DraftInvalid(Exception):
    """The drafter answered, but not with a valid ``{subject, body}``: counts as an attempt."""


class DraftUnavailable(Exception):
    """The drafter could not answer (timeout, API error, no prompt): use the template now."""


class TemplateViolation(RuntimeError):
    """The §8.4 template broke the validator: a bug, never saved."""


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    subject: str
    body: str
    generator: str  # "llm" or "template"
    violations: tuple[str, ...]  # of the saved draft: always empty
    attempts: tuple[tuple[str, ...], ...]  # violation codes of each rejected LLM attempt


def generate_draft(
    facts: FactBlock,
    writer: DraftWriter | None,
    *,
    max_regenerations: int,
    max_words: int,
    invoice_regex: str,
    recipient: str | None,
    sender_domain: str | None,
) -> DraftOutcome:
    def check(subject: str, body: str) -> tuple[str, ...]:
        return check_draft(
            subject,
            body,
            facts,
            max_words=max_words,
            invoice_regex=invoice_regex,
            recipient=recipient,
            sender_domain=sender_domain,
        )

    attempts: list[tuple[str, ...]] = []
    if writer is not None:
        for _ in range(1 + max_regenerations):
            try:
                subject, body = writer(facts.to_json())
            except DraftInvalid:
                attempts.append((INVALID_OUTPUT,))
                continue
            except DraftUnavailable:
                attempts.append((UNAVAILABLE,))
                break
            subject = subject.replace(CUSTOMER_PLACEHOLDER, facts.customer_name)
            body = body.replace(CUSTOMER_PLACEHOLDER, facts.customer_name)
            violations = check(subject, body)
            if not violations:
                return DraftOutcome(subject, body, "llm", (), tuple(attempts))
            attempts.append(violations)

    subject, body = render_template(facts)
    violations = check(subject, body)
    if violations:
        raise TemplateViolation(f"the §8.4 template violates {', '.join(violations)}")
    return DraftOutcome(subject, body, "template", (), tuple(attempts))
