"""One agent tick (SPEC §18.6; docs/DECISIONS.md D-024).

``run_tick`` selects the highest-priority overdue cases, opens or updates each case through
the §18.2 state machine, plans (an optional planner whose output is validated, else the rule
planner), runs every step under the autonomy level and the policy engine, and records the run
and each step. Persistence goes through the ``AgentStore`` port, so this module never touches
the database; time comes from the caller (``now_local`` from the injected clock).
"""

import dataclasses
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from receivables.agent.cases import (
    Assessed,
    BecameOverdue,
    CaseState,
    CaseTrigger,
    DisputeOpened,
    DisputeResolved,
    EscalateExecuted,
    MessageSent,
    PaidInFull,
    transition,
)
from receivables.agent.drafting import DraftWriter, generate_draft
from receivables.agent.facts import AgentFacts
from receivables.agent.plans import PlanRejection, validate_plan
from receivables.agent.policy import L2Limits, SendDraft, evaluate_send, send_facts_from
from receivables.agent.rules_planner import Plan, PlanStep, plan_case
from receivables.agent.tools import TOOL_REGISTRY
from receivables.core.config import EngineConfig
from receivables.core.draft_validator import check_draft
from receivables.core.drafts import build_fact_block

__all__ = [
    "AgentStore",
    "DraftRecord",
    "PlanSource",
    "PlannerError",
    "RunSummary",
    "StepRecord",
    "TickConfig",
    "run_tick",
]

log = logging.getLogger("receivables.agent")

DOC_LABELS = {
    "delivery_note": "delivery note",
    "po_copy": "purchase order copy",
    "contract_rate": "contract rate",
    "statement": "statement of account",
}
INTERNAL_TASK_DAYS = 3
MAX_CASES_PER_TICK = 20  # §18.6 step 1
STALE_DRAFT = "STALE_DRAFT"  # the draft no longer matches today's facts


class PlannerError(Exception):
    """The planner timed out or returned something that is not a plan (§18.6 step 7)."""


class PlanSource(Protocol):
    """A planner model behind a port (the LLM planner: ``adapters.llm.planner``, D-037)."""

    @property
    def model(self) -> str | None: ...

    def propose(self, observation: Mapping[str, Any]) -> object: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TickConfig:
    engine: EngineConfig
    l2: L2Limits
    max_cases_per_tick: int
    max_steps_per_case: int
    max_words: int
    invoice_regex: str
    sender_name: str
    max_regenerations: int = 2
    sender_domain: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftRecord:
    draft_id: str
    customer_id: str
    invoice_numbers: tuple[str, ...]
    tone: int
    dispute_label: str | None
    subject: str
    body: str
    status: str
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRecord:
    step_id: str
    run_id: str
    invoice_number: str
    seq: int
    tool: str
    args: Mapping[str, Any]
    reason: str
    policy_allowed: bool | None
    policy_reasons: tuple[str, ...]
    status: str
    result: Mapping[str, Any] | None
    state_before: str | None = None  # the case state around the step, for its audit row
    state_after: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    as_of: date
    autonomy_level: int
    planner: str
    fallback: bool
    model: str | None
    cases_considered: int
    steps_executed: int
    steps_denied: int
    case_errors: int


class AgentStore(Protocol):
    """Everything a tick reads and writes, inside one transaction."""

    def load_facts(self, as_of: date, config: EngineConfig) -> AgentFacts: ...
    def case_states(self) -> Mapping[str, CaseState]: ...
    def save_case(
        self,
        invoice_number: str,
        before: CaseState | None,
        after: CaseState,
        trigger: str,
        run_id: str,
        event_id: str | None = None,
    ) -> None: ...
    def applied_events(self) -> set[tuple[str, str]]: ...
    def mark_event(self, invoice_number: str, event_id: str, trigger: str, run_id: str) -> None: ...
    def set_followup(self, invoice_number: str, on: date) -> None: ...
    def savepoint(self) -> AbstractContextManager[None]: ...
    def start_run(self, summary: RunSummary, now: datetime) -> None: ...
    def finish_run(self, summary: RunSummary) -> None: ...
    def add_step(self, step: StepRecord) -> None: ...
    def add_draft(self, draft_id: str, *, fields: Mapping[str, Any]) -> None: ...
    def get_draft(self, draft_id: str) -> DraftRecord | None: ...
    def add_task(self, task_id: str, *, fields: Mapping[str, Any]) -> None: ...
    def add_approval(self, approval_id: str, *, step_id: str, kind: str, payload: Any) -> None: ...
    def add_sandbox_mail(self, mail_id: str, *, fields: Mapping[str, Any]) -> None: ...
    def sends_on(self, local_date: date, timezone: str) -> int: ...
    def outbound_dates(self, customer_id: str, timezone: str) -> tuple[date, ...]: ...
    def audit(self, action: str, entity_type: str, entity_id: str, after: Any) -> None: ...


@dataclass
class _Tick:
    store: AgentStore
    facts: AgentFacts
    config: TickConfig
    run_id: str
    now_local: datetime
    autonomy: int
    seq: int = 0
    executed: int = 0
    denied: int = 0
    states: dict[str, CaseState] = field(default_factory=dict)
    applied_events: set[tuple[str, str]] = field(default_factory=set)
    kill_switch: bool = False
    drafter: DraftWriter | None = None  # None: the §8.4 template only (D-036)

    @property
    def persist(self) -> bool:
        """L0 shadow and the kill switch change nothing (§18.4, §18.5); states stay in memory."""
        return self.autonomy > 0 and not self.kill_switch

    # Writes that reference the step row (approvals), run once that row exists.
    after_step: list[Callable[[], None]] = field(default_factory=list)

    def apply(self, invoice: str, trigger: CaseTrigger, event_id: str | None = None) -> Any:
        before = self.states.get(invoice)
        result = transition(before, trigger)
        if result.applied and result.to_state is not None and result.to_state != before:
            if self.persist:
                self.store.save_case(
                    invoice,
                    before,
                    result.to_state,
                    type(trigger).__name__,
                    self.run_id,
                    event_id,
                )
            self.states[invoice] = result.to_state
        elif event_id is not None and self.persist:  # considered, even without a change
            self.store.mark_event(invoice, event_id, type(trigger).__name__, self.run_id)
        if event_id is not None and self.persist:
            self.applied_events.add((invoice, event_id))
        return result


def run_tick(
    store: AgentStore,
    *,
    as_of: date,
    now_local: datetime,
    autonomy_level: int,
    config: TickConfig,
    planner: PlanSource | None = None,
    drafter: DraftWriter | None = None,
    new_run_id: Callable[[], str] = lambda: f"RUN-{uuid4().hex[:12]}",
) -> RunSummary:
    """One tick (§18.6). The caller commits or rolls back the store's transaction.

    ``drafter`` writes LLM drafts (§8.2); without it every draft is the §8.4 template.
    """
    if now_local.tzinfo is None:
        raise ValueError("now_local must carry a time zone")
    if autonomy_level not in (0, 1, 2):
        raise ValueError("autonomy_level is 0, 1 or 2")
    facts = store.load_facts(as_of, config.engine)
    run_id = new_run_id()
    tick = _Tick(store, facts, config, run_id, now_local, autonomy_level)
    tick.kill_switch = config.l2.kill_switch  # read once, at the start of the tick
    tick.drafter = drafter
    tick.states = dict(store.case_states())
    tick.applied_events = set(store.applied_events())
    summary = RunSummary(
        run_id, as_of, autonomy_level, "llm" if planner else "rules", False,
        planner.model if planner else None, 0, 0, 0, 0,
    )  # fmt: skip
    store.start_run(summary, now_local)

    case_errors = 0
    # Cases whose invoice is no longer open are resolved (§18.2 "open amount reaches 0").
    for invoice, state in sorted(tick.states.items()):
        row = facts.rows.get(invoice)
        if state is not CaseState.RESOLVED and row is not None and row.open_amount == 0:
            try:
                with store.savepoint():
                    tick.apply(invoice, PaidInFull())
            except Exception as error:
                tick.states[invoice] = state
                case_errors += 1
                store.audit(
                    "agent.case_failed",
                    "invoice",
                    invoice,
                    {"run_id": run_id, "error": _code(error)},
                )

    ordered = sorted(facts.scored.values(), key=lambda s: (-s.priority_value, s.invoice_number))
    budget = min(config.max_cases_per_tick, MAX_CASES_PER_TICK)
    selected = [s.invoice_number for s in ordered[:budget]]
    fallback = False
    for invoice in selected:
        saved = (dict(tick.states), set(tick.applied_events), tick.seq, tick.executed, tick.denied)
        try:
            with store.savepoint():  # a failing case is rolled back as a whole
                fallback |= _run_case(tick, invoice, planner)
        except Exception as error:  # one bad case never stops the tick (§18.6 step 7)
            tick.states, tick.applied_events, tick.seq, tick.executed, tick.denied = saved
            case_errors += 1  # queued approval writes are dropped when the next case starts
            store.audit(
                "agent.case_failed", "invoice", invoice, {"run_id": run_id, "error": _code(error)}
            )
    summary = RunSummary(
        run_id, as_of, autonomy_level, summary.planner, fallback, summary.model,
        len(selected), tick.executed, tick.denied, case_errors,
    )  # fmt: skip
    store.finish_run(summary)
    log.info(
        "agent tick finished",
        extra={"run_id": run_id, "cases": len(selected), "executed": tick.executed},
    )
    return summary


def _code(error: Exception) -> str:
    """An error code for the trace: the exception type only, never its message (may hold data)."""
    return type(error).__name__


def _open_case(tick: _Tick, invoice: str) -> None:
    """Create and assess the case, then apply the latest dispute event once (§18.2)."""
    tick.apply(invoice, BecameOverdue())
    tick.apply(invoice, Assessed())
    disputes = [
        e
        for e in tick.facts.events.get(invoice, ())
        if e.event_type in {"DISPUTE_OPENED", "DISPUTE_RESOLVED"}
    ]
    if not disputes:
        return
    latest = max(disputes, key=lambda e: (e.event_date, e.event_id))
    if (invoice, latest.event_id) in tick.applied_events:
        return  # already applied: later state changes (e.g. escalation) stand
    trigger = DisputeOpened() if latest.event_type == "DISPUTE_OPENED" else DisputeResolved()
    tick.apply(invoice, trigger, latest.event_id)


def _read(tool: str, args: Mapping[str, Any], facts: AgentFacts) -> Any:
    spec = TOOL_REGISTRY[tool]
    if spec.handler is None:  # pragma: no cover - every read tool has a handler (T-AGT-03)
        raise RuntimeError(f"{tool} has no handler")
    return spec.handler(spec.args_model.model_validate(dict(args)), facts)


def _observation(tick: _Tick, invoice: str) -> dict[str, Any]:
    """§18.6 step 2: get_case and get_customer_profile (no customer names, emails or TRNs)."""
    customer = tick.facts.rows[invoice].customer_id
    case = _read("get_case", {"invoice_number": invoice}, tick.facts)
    profile = _read("get_customer_profile", {"customer_id": customer}, tick.facts)
    return {"case": case.data, "customer": profile.data}


def _plan(tick: _Tick, invoice: str, planner: PlanSource | None) -> tuple[Plan, bool]:
    """The plan to run and whether the rule planner replaced the planner's output."""
    if planner is None or tick.kill_switch:  # no planner call while the switch is on
        return plan_case(tick.facts, invoice), False
    asked = False
    try:
        observation = _observation(tick, invoice)
        asked = True
        raw = planner.propose(observation)
    except Exception as error:  # any planner failure falls back (§18.6 step 7)
        _plan_received(tick, invoice, planner, "failed", asked=asked)
        tick.store.audit(
            "agent.planner_failed",
            "invoice",
            invoice,
            {"run_id": tick.run_id, "error": _code(error)},
        )
        return plan_case(tick.facts, invoice), True
    checked = validate_plan(raw, invoice, max_steps=tick.config.max_steps_per_case)
    _plan_received(
        tick, invoice, planner, "rejected" if isinstance(checked, PlanRejection) else "used"
    )
    if isinstance(checked, PlanRejection):
        tick.store.audit(
            "agent.plan_rejected",
            "invoice",
            invoice,
            {"run_id": tick.run_id, "status": checked.status, "reasons": list(checked.reasons),
             "tools": list(checked.tools)},
        )  # fmt: skip
        return plan_case(tick.facts, invoice), True
    return checked, False


def _plan_received(
    tick: _Tick, invoice: str, planner: PlanSource, use: str, *, asked: bool = True
) -> None:
    """§18.6 step 6: model, latency and what became of the plan, one audit row per call.

    The planner reports its own call (``last_call``: model role, prompt version, attempts,
    latency); ``agent/`` reads no clock (D-037).
    """
    call = getattr(planner, "last_call", None) if asked else None  # never another case's call
    tick.store.audit(
        "agent.plan_received",
        "invoice",
        invoice,
        {"run_id": tick.run_id, "plan": use, "model": planner.model}
        | (dict(call) if isinstance(call, Mapping) else {}),
    )


def _run_case(tick: _Tick, invoice: str, planner: PlanSource | None) -> bool:
    tick.after_step.clear()
    _open_case(tick, invoice)
    plan, fell_back = _plan(tick, invoice, planner)
    drafts: dict[int, str] = {}  # plan position -> draft id created by that step
    for position, step in enumerate(plan.steps, start=1):
        tick.seq += 1
        step_id = f"{tick.run_id}-S{tick.seq:03d}"
        before = tick.states.get(invoice)
        record = _run_step(tick, invoice, step, step_id, drafts)
        after = tick.states.get(invoice)
        if tick.persist:  # otherwise nothing about the case was saved; do not imply it was
            record = dataclasses.replace(
                record,
                state_before=None if before is None else before.value,
                state_after=None if after is None else after.value,
            )
        if record.status == "executed" and step.tool == "draft_message" and record.result:
            drafts[position] = str(record.result["draft_id"])
        tick.executed += record.status == "executed"
        tick.denied += record.policy_allowed is False
        tick.store.add_step(record)
        for write in tick.after_step:
            write()
        tick.after_step.clear()
    return fell_back


def _record(
    tick: _Tick,
    invoice: str,
    step: PlanStep,
    step_id: str,
    status: str,
    result: Mapping[str, Any] | None,
    *,
    allowed: bool | None = None,
    reasons: tuple[str, ...] = (),
) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        run_id=tick.run_id,
        invoice_number=invoice,
        seq=tick.seq,
        tool=step.tool,
        args=step.json_args(),
        reason=step.reason,
        policy_allowed=allowed,
        policy_reasons=reasons,
        status=status,
        result=result,
    )


def _run_step(
    tick: _Tick, invoice: str, step: PlanStep, step_id: str, drafts: Mapping[int, str]
) -> StepRecord:
    if tick.kill_switch:  # nothing executes (§18.5); the trace says why
        return _record(
            tick, invoice, step, step_id, "denied", None,
            allowed=False, reasons=("POLICY_KILL_SWITCH",),
        )  # fmt: skip
    if tick.autonomy == 0:  # L0 shadow: record only
        return _record(tick, invoice, step, step_id, "skipped", None)
    states, events, pending = dict(tick.states), set(tick.applied_events), len(tick.after_step)
    try:
        with tick.store.savepoint():  # a step's writes stand or fall together
            return _execute(tick, invoice, step, step_id, drafts)
    except Exception as error:  # a failing step is recorded, the case continues
        tick.states, tick.applied_events = states, events
        del tick.after_step[pending:]
        return _record(tick, invoice, step, step_id, "failed", {"error": _code(error)})


def _execute(
    tick: _Tick, invoice: str, step: PlanStep, step_id: str, drafts: Mapping[int, str]
) -> StepRecord:
    spec = TOOL_REGISTRY[step.tool]
    if step.tool == "send_email":
        return _send_email(tick, invoice, step, step_id, drafts)
    if spec.kind == "read":
        out = _read(step.tool, step.json_args(), tick.facts)
        status = "executed" if out.ok else "failed"
        return _record(tick, invoice, step, step_id, status, out.data or {"error": out.error})
    result = _EFFECTS[step.tool](tick, invoice, step.json_args(), step_id)
    status = "failed" if "error" in result else "executed"
    return _record(tick, invoice, step, step_id, status, result)


# --- effects of the internal tools (L1 and above) ----------------------------------------


def _draft_message(tick: _Tick, invoice: str, args: dict[str, Any], step_id: str) -> dict[str, Any]:
    facts = tick.facts
    rows = [facts.rows[n] for n in args["invoice_numbers"]]
    customer = facts.customers[rows[0].customer_id]
    block = build_fact_block(
        rows,
        customer_name=customer.customer_name,
        sender_name=tick.config.sender_name,
        as_of=facts.as_of,
        tone=args["tone"],
        dispute_label=args.get("dispute_label"),
    )
    outcome = generate_draft(
        block,
        tick.drafter,
        max_regenerations=tick.config.max_regenerations,
        max_words=tick.config.max_words,
        invoice_regex=tick.config.invoice_regex,
        recipient=customer.contact_email,
        sender_domain=tick.config.sender_domain,
    )
    draft_id = f"{step_id}-D"
    tick.store.add_draft(
        draft_id,
        fields={
            "invoice_numbers": list(args["invoice_numbers"]),
            "customer_id": customer.customer_id,
            "tone": args["tone"],
            "subject": outcome.subject,
            "body": outcome.body,
            "facts": block.to_json(),
            "generator": outcome.generator,
            "violations": list(outcome.violations),
        },
    )
    return {
        "draft_id": draft_id,
        "generator": outcome.generator,
        "violations": list(outcome.violations),
        # Why earlier LLM attempts were sent back: violation codes only, never text (§8.2).
        "attempts": [list(codes) for codes in outcome.attempts],
        # Which model role and prompt wrote an LLM draft (§18.6 step 6); never a model id.
        "model": getattr(tick.drafter, "model", None) if outcome.generator == "llm" else None,
        "prompt_version": (
            getattr(tick.drafter, "prompt_version", None) if outcome.generator == "llm" else None
        ),
    }


def _task(tick: _Tick, step_id: str, invoice: str | None, owner: str, text: str, due: date) -> str:
    task_id = f"{step_id}-T"
    tick.store.add_task(
        task_id,
        fields={
            "invoice_number": invoice,
            "owner_role": owner,
            "text": text,
            "due_date": due.isoformat(),
            "created_by": "agent",
        },
    )
    return task_id


def _create_task(tick: _Tick, invoice: str, args: dict[str, Any], step_id: str) -> dict[str, Any]:
    due = date.fromisoformat(args["due_date"])
    return {"task_id": _task(tick, step_id, invoice, args["owner_role"], args["text"], due)}


def _request_document(
    tick: _Tick, invoice: str, args: dict[str, Any], step_id: str
) -> dict[str, Any]:
    text = f"Provide the {DOC_LABELS[args['doc_type']]} for {invoice}."
    due = tick.facts.as_of + timedelta(days=INTERNAL_TASK_DAYS)
    return {"task_id": _task(tick, step_id, invoice, args["owner_role"], text, due)}


def _schedule_followup(
    tick: _Tick, invoice: str, args: dict[str, Any], step_id: str
) -> dict[str, Any]:
    on = tick.facts.as_of + timedelta(days=args["days"])
    tick.store.set_followup(invoice, on)
    return {"next_followup": on.isoformat()}


def _escalate(tick: _Tick, invoice: str, args: dict[str, Any], step_id: str) -> dict[str, Any]:
    result = tick.apply(invoice, EscalateExecuted())
    if not result.applied:
        return {"error": "CASE_NOT_ESCALATABLE"}
    task_id = None
    if result.escalation_task:  # one task per entry into ESCALATED (D-019)
        days = tick.facts.rows[invoice].days_overdue
        text = f"Escalated: {invoice} is {days} days overdue."
        task_id = _task(tick, step_id, invoice, args["to_role"], text, tick.facts.as_of)
    return {"state": CaseState.ESCALATED.value, "task_id": task_id}


def _propose_allocation(
    tick: _Tick, invoice: str, args: dict[str, Any], step_id: str
) -> dict[str, Any]:
    customer = tick.facts.rows[invoice].customer_id
    payment = next(
        (
            p
            for p in tick.facts.unallocated_payments
            if p.payment_id == args["payment_id"] and p.customer_id == customer
        ),
        None,
    )
    if payment is None:
        return {"error": "UNKNOWN_PAYMENT"}
    approval_id = f"{step_id}-A"
    payload = {"payment_id": payment.payment_id, "invoice_number": invoice}
    tick.after_step.append(
        lambda: tick.store.add_approval(
            approval_id, step_id=step_id, kind="allocation", payload=payload
        )
    )
    return {"approval_id": approval_id}


def _not_available(*_: Any) -> dict[str, Any]:
    return {"error": "NOT_AVAILABLE"}  # classify_message arrives with the LLM (S3.06)


_EFFECTS: dict[str, Callable[[_Tick, str, dict[str, Any], Any], dict[str, Any]]] = {
    "draft_message": _draft_message,
    "create_task": _create_task,
    "request_internal_document": _request_document,
    "schedule_followup": _schedule_followup,
    "escalate": _escalate,
    "propose_allocation": _propose_allocation,
    "classify_message": _not_available,
}


def _recheck(tick: _Tick, draft: DraftRecord) -> tuple[str, ...]:
    """The draft's violations against today's facts, not those saved when it was made."""
    facts = tick.facts
    try:
        rows = [facts.rows[n] for n in draft.invoice_numbers]
        block = build_fact_block(
            rows,
            customer_name=facts.customers[draft.customer_id].customer_name,
            sender_name=tick.config.sender_name,
            as_of=facts.as_of,
            tone=draft.tone,
            dispute_label=draft.dispute_label,
        )
    except (KeyError, ValueError):  # an invoice is no longer overdue, or unknown
        return tuple(sorted({*draft.violations, STALE_DRAFT}))
    found = check_draft(
        draft.subject,
        draft.body,
        block,
        max_words=tick.config.max_words,
        invoice_regex=tick.config.invoice_regex,
        recipient=facts.customers[draft.customer_id].contact_email,
        sender_domain=tick.config.sender_domain,
    )
    return tuple(sorted({*draft.violations, *found}))


def _send_email(
    tick: _Tick, invoice: str, step: PlanStep, step_id: str, drafts: Mapping[int, str]
) -> StepRecord:
    args = step.json_args()
    ref = args.get("draft_ref")
    draft_id = drafts.get(int(ref.split(":")[1])) if ref else args.get("draft_id")
    draft = tick.store.get_draft(draft_id) if draft_id else None
    customer_id = tick.facts.rows[invoice].customer_id
    if (
        draft is None
        or draft.status != "draft"  # rejected, approved or already sent drafts are not reused
        or draft.customer_id != customer_id
        or invoice not in draft.invoice_numbers
    ):
        return _record(tick, invoice, step, step_id, "failed", {"error": "DRAFT_NOT_AVAILABLE"})
    l2 = tick.config.l2
    customer = tick.facts.customers[customer_id]
    recipient = customer.contact_email or ""  # never from the plan (§18.3)
    local_today = tick.now_local.astimezone(ZoneInfo(l2.timezone)).date()
    case = send_facts_from(
        tick.facts,
        draft.invoice_numbers,
        sends_today=tick.store.sends_on(local_today, l2.timezone),
        timezone=l2.timezone,
        outbound_dates=tick.store.outbound_dates(customer_id, l2.timezone),
    )
    decision = evaluate_send(
        case,
        SendDraft(
            invoice_numbers=draft.invoice_numbers,
            tone=draft.tone,
            recipient=recipient,
            violations=_recheck(tick, draft),
        ),
        tick.now_local,
        l2,
    )
    if decision.allowed and tick.autonomy == 2:
        mail_id = f"{step_id}-M"
        tick.store.add_sandbox_mail(
            mail_id, fields={"draft_id": draft.draft_id, "to": recipient, "at": tick.now_local}
        )
        tick.apply(invoice, MessageSent())
        return _record(
            tick, invoice, step, step_id, "executed",
            {"mail_id": mail_id, "gateway": "sandbox", "draft_id": draft.draft_id},
            allowed=True, reasons=(),
        )  # fmt: skip
    approval_id = f"{step_id}-A"
    payload = {
        "draft_id": draft.draft_id,
        "policy_allowed": decision.allowed,
        "reasons": list(decision.reasons),
    }
    tick.after_step.append(
        lambda: tick.store.add_approval(
            approval_id, step_id=step_id, kind="send_email", payload=payload
        )
    )
    return _record(
        tick, invoice, step, step_id, "converted_to_approval",
        {"approval_id": approval_id, "draft_id": draft.draft_id},
        allowed=decision.allowed, reasons=decision.reasons,
    )  # fmt: skip
