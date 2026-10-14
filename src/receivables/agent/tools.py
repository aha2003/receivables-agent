"""The closed tool registry (SPEC §18.3; docs/DECISIONS.md D-020).

``TOOL_REGISTRY`` holds exactly the 11 tools of §18.3 and is read-only. Every tool has a strict
Pydantic argument model: unknown fields are refused, types are not coerced, and arguments are
only identifiers, enums, bounded integers and one bounded task text. No argument carries an
amount, a recipient address, SQL or a path, and no tool may write invoices, payments,
customers or events (``ToolSpec.writes``).

The three read tools are implemented here as pure functions over ``AgentFacts``. The other
tools' effects arrive with the steps that build them (drafts S2.05/S3.07, internal writes
S2.07, classification S3.06, sending S3.09, allocation S3.11); their ``handler`` is None.
Read tool outputs never contain customer names, emails, TRNs or licence numbers.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from receivables.agent.facts import DEFAULT_FOLLOWUP_GAP_DAYS, AgentFacts
from receivables.core.money import from_fils
from receivables.core.queue import HEURISTIC_NOTE, next_action
from receivables.core.scoring import active_dispute_label, is_broken_promise, round_half_up
from receivables.core.validation import DISPUTE_EVENT_LABELS

__all__ = [
    "LEDGER_TABLES",
    "MAX_PLAN_STEPS",
    "ROLES",
    "TOOL_REGISTRY",
    "ToolResult",
    "ToolSpec",
]

MAX_PLAN_STEPS = 5  # §18.6
ROLES = ("finance", "sales", "operations", "management")
# Tables holding ledger facts: no tool may write them (§18.3 "Not tools").
LEDGER_TABLES = frozenset({"invoices", "payments", "customers", "events"})
MESSAGES_IN_CASE = 5

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _identifier(value: Any) -> Any:
    if isinstance(value, str) and (
        _CONTROL.search(value) or value != value.strip() or not 1 <= len(value) <= 64
    ):
        raise ValueError("identifiers are 1 to 64 characters without control characters")
    return value


def _iso_date(value: Any) -> Any:
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("dates are written YYYY-MM-DD")
        return date.fromisoformat(value)
    return value


def _task_text(value: Any) -> Any:
    if isinstance(value, str) and _CONTROL.search(value.replace("\n", "")):
        raise ValueError("task text may not contain control characters other than newlines")
    return value


def _dispute_label(value: Any) -> Any:
    if isinstance(value, str) and value not in DISPUTE_EVENT_LABELS:
        raise ValueError(f"dispute_label must be one of {sorted(DISPUTE_EVENT_LABELS)}")
    return value


Identifier = Annotated[str, BeforeValidator(_identifier)]
Role = Literal["finance", "sales", "operations", "management"]
DisputeLabel = Annotated[str, BeforeValidator(_dispute_label)]
IsoDate = Annotated[date, BeforeValidator(_iso_date)]
TaskText = Annotated[str, BeforeValidator(_task_text), Field(min_length=1, max_length=500)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GetCaseArgs(_Args):
    invoice_number: Identifier


class GetCustomerProfileArgs(_Args):
    customer_id: Identifier


class GetUnallocatedCashArgs(_Args):
    customer_id: Identifier


class ClassifyMessageArgs(_Args):
    message_id: Identifier


class DraftMessageArgs(_Args):
    invoice_numbers: list[Identifier] = Field(min_length=1, max_length=10)
    tone: int = Field(ge=1, le=3)
    dispute_label: DisputeLabel | None = None

    @model_validator(mode="after")
    def _distinct(self) -> "DraftMessageArgs":
        if len(set(self.invoice_numbers)) != len(self.invoice_numbers):
            raise ValueError("invoice_numbers must be distinct")
        return self


class RequestInternalDocumentArgs(_Args):
    invoice_number: Identifier
    doc_type: Literal["delivery_note", "po_copy", "contract_rate", "statement"]
    owner_role: Role


class ProposeAllocationArgs(_Args):
    payment_id: Identifier
    invoice_number: Identifier


class CreateTaskArgs(_Args):
    owner_role: Role
    text: TaskText
    due_date: IsoDate


class ScheduleFollowupArgs(_Args):
    invoice_number: Identifier
    days: int = Field(ge=1, le=30)


class EscalateArgs(_Args):
    invoice_number: Identifier
    to_role: Role


class SendEmailArgs(_Args):
    """Exactly one of ``draft_id`` or ``draft_ref`` (``step:N``, an earlier plan step; §18.3)."""

    draft_id: Identifier | None = None
    draft_ref: Annotated[str, Field(pattern=rf"^step:[1-{MAX_PLAN_STEPS}]$")] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SendEmailArgs":
        if (self.draft_id is None) == (self.draft_ref is None):
            raise ValueError("give exactly one of draft_id or draft_ref")
        return self


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    data: Mapping[str, Any] | None = None
    error: str | None = None


Kind = Literal["read", "analysis", "prepare", "internal_write", "external"]
Handler = Callable[[Any, AgentFacts], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    kind: Kind
    min_autonomy: int
    args_model: type[_Args]
    writes: frozenset[str] = field(default_factory=frozenset)
    policy_gated: bool = False  # passes the §18.5 policy engine before it may execute
    handler: Handler | None = None


# --- read tools ---------------------------------------------------------------------------


def _money(fils: int) -> str:
    return str(from_fils(fils))


def _unknown(kind: str) -> ToolResult:
    return ToolResult(ok=False, error=f"UNKNOWN_{kind.upper()}")


def _playbook(facts: AgentFacts, customer_id: str) -> dict[str, Any]:
    p = facts.playbooks.get(customer_id)
    if p is None:
        return {
            "promise_kept_rate": None,
            "promises_counted": 0,
            "avg_days_to_reply": None,
            "replies_counted": 0,
            "followup_gap_days": DEFAULT_FOLLOWUP_GAP_DAYS,
        }
    return {
        "promise_kept_rate": None if p.promise_kept_rate is None else str(p.promise_kept_rate),
        "promises_counted": p.promises_counted,
        "avg_days_to_reply": None if p.avg_days_to_reply is None else str(p.avg_days_to_reply),
        "replies_counted": p.replies_counted,
        "followup_gap_days": p.followup_gap_days,
    }


def get_case(args: GetCaseArgs, facts: AgentFacts) -> ToolResult:
    row = facts.rows.get(args.invoice_number)
    if row is None:
        return _unknown("invoice")
    events = facts.events.get(row.invoice_number, ())
    is_open = row.open_amount > 0
    scored = facts.scored.get(row.invoice_number)
    item = facts.queue_items.get(row.invoice_number)
    cash = facts.unallocated_by_customer.get(row.customer_id, 0)
    messages = [m for m in facts.messages if m.invoice_number == row.invoice_number]
    return ToolResult(
        ok=True,
        data={
            "as_of": facts.as_of.isoformat(),
            "invoice_number": row.invoice_number,
            "customer_id": row.customer_id,
            "invoice_date": row.invoice_date.isoformat(),
            "due_date": row.due_date.isoformat(),
            "amount": _money(row.amount),
            "open_amount": _money(row.open_amount),
            "is_open": is_open,
            "days_overdue": row.days_overdue,
            "bucket": row.bucket,
            "settled_date": None if row.settled_date is None else row.settled_date.isoformat(),
            "flags": {
                "active_dispute_label": active_dispute_label(events, facts.as_of),
                "broken_promise": is_broken_promise(events, facts.as_of, is_open=is_open),
                "unallocated_cash_on_account": _money(cash) if cash > 0 else None,
            },
            "priority": None
            if scored is None
            else {
                "risk_score": str(scored.risk_score),
                "band": scored.band,
                "priority_value": str(scored.priority_value),
                "rank": None if item is None else item.rank,
                "next_action": next_action(scored),
                "note": HEURISTIC_NOTE,
            },
            "events": [
                {
                    "event_id": e.event_id,
                    "event_type": e.event_type,
                    "event_date": e.event_date.isoformat(),
                    "label": e.label,
                    "promise_date": None if e.promise_date is None else e.promise_date.isoformat(),
                }
                for e in events
            ],
            "messages": [
                {
                    "message_id": m.message_id,
                    "direction": m.direction,
                    "sent_at": m.sent_at,
                    "language": m.language,
                }
                for m in messages[-MESSAGES_IN_CASE:]
            ],
            "playbook": _playbook(facts, row.customer_id),
        },
    )


def get_customer_profile(args: GetCustomerProfileArgs, facts: AgentFacts) -> ToolResult:
    customer = facts.customers.get(args.customer_id)
    if customer is None:
        return _unknown("customer")
    rows = [r for r in facts.rows.values() if r.customer_id == customer.customer_id]
    open_rows = [r for r in rows if r.open_amount > 0]
    overdue = [r for r in open_rows if r.days_overdue is not None and r.days_overdue > 0]
    history = facts.history.customers.get(customer.customer_id)
    return ToolResult(
        ok=True,
        data={
            "as_of": facts.as_of.isoformat(),
            "customer_id": customer.customer_id,
            "payment_terms_days": customer.payment_terms_days,
            "history": {
                "window_days": facts.config.history_window_days,
                "paid": 0 if history is None else history.paid,
                "late": 0 if history is None else history.late,
                "late_rate": None if history is None else str(round_half_up(history.late_rate, 4)),
            },
            "open_invoices": len(open_rows),
            "open_total": _money(sum(r.open_amount for r in open_rows)),
            "overdue_invoices": len(overdue),
            "overdue_total": _money(sum(r.open_amount for r in overdue)),
            "unallocated_total": _money(facts.unallocated_by_customer.get(customer.customer_id, 0)),
            "playbook": _playbook(facts, customer.customer_id),
        },
    )


def get_unallocated_cash(args: GetUnallocatedCashArgs, facts: AgentFacts) -> ToolResult:
    if args.customer_id not in facts.customers:
        return _unknown("customer")
    payments = [p for p in facts.unallocated_payments if p.customer_id == args.customer_id]
    return ToolResult(
        ok=True,
        data={
            "as_of": facts.as_of.isoformat(),
            "customer_id": args.customer_id,
            "payments": [
                {
                    "payment_id": p.payment_id,
                    "payment_date": p.payment_date.isoformat(),
                    "payment_type": p.payment_type,
                    "amount": _money(p.amount),
                }
                for p in payments
            ],
            "total": _money(facts.unallocated_by_customer.get(args.customer_id, 0)),
        },
    )


# --- the registry ---------------------------------------------------------------------------

_SPECS = (
    ToolSpec("get_case", "read", 0, GetCaseArgs, handler=get_case),
    ToolSpec(
        "get_customer_profile", "read", 0, GetCustomerProfileArgs, handler=get_customer_profile
    ),
    ToolSpec(
        "get_unallocated_cash", "read", 0, GetUnallocatedCashArgs, handler=get_unallocated_cash
    ),
    ToolSpec(
        "classify_message", "analysis", 0, ClassifyMessageArgs, frozenset({"classifications"})
    ),
    ToolSpec("draft_message", "prepare", 1, DraftMessageArgs, frozenset({"drafts"})),
    ToolSpec(
        "request_internal_document",
        "prepare",
        1,
        RequestInternalDocumentArgs,
        frozenset({"tasks", "drafts"}),
    ),
    ToolSpec("propose_allocation", "prepare", 1, ProposeAllocationArgs, frozenset({"approvals"})),
    ToolSpec("create_task", "internal_write", 1, CreateTaskArgs, frozenset({"tasks"})),
    ToolSpec("schedule_followup", "internal_write", 1, ScheduleFollowupArgs, frozenset({"cases"})),
    ToolSpec("escalate", "internal_write", 1, EscalateArgs, frozenset({"cases", "tasks"})),
    ToolSpec(
        "send_email",
        "external",
        2,
        SendEmailArgs,
        frozenset({"mail_outbox", "drafts"}),
        policy_gated=True,
    ),
)

TOOL_REGISTRY: Mapping[str, ToolSpec] = MappingProxyType({s.name: s for s in _SPECS})
