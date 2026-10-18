"""The simulator's arms and day-60 metrics (SPEC §19.2; S4.03; D-050). SIMULATION — MECHANICS,
NOT EVIDENCE.

``run_arms`` runs each arm in its own fresh ``sim_<seed>_<arm>.db`` (the S4.01 company), on a
simulated clock, for days 0 to ``scenario.days`` (2026-11-02 to 2027-01-01), and writes one
results JSON. Each day, in this order:

1. 09:00, every day: the customers act (S4.02). Payments and replies go in through the strict
   importer; a reply is only its text (``CustomerReply.label`` is ground truth and never reaches
   the classifier or the planner). In the agent arms the inbox classifies it (fake mode: no
   model, so "unclear, needs review").
2. 09:30, working days: the simulated human acts on what has waited at least one day
   (D-050): it accepts each reply with its true label (A2, A3; option 2a); allocates the
   customer's cash to an invoice claimed ALREADY_PAID one working day after accepting it
   (option 4a); resolves a dispute 3 working days after the agent's first document request for
   it, as a ``DISPUTE_RESOLVED`` event (option 3a); and approves and marks sent every draft
   created on an earlier day (options 1a, 5a), after checking it again with the draft validator
   and recording ``evaluate_send`` for it; it rejects a draft that is out of date, or about an
   invoice messaged in the last ``min_days_between_contacts`` days.
3. 10:00, working days: the arm acts. A0 nothing; A1 a template reminder draft (tone by age)
   for every overdue invoice not messaged or drafted in the last 7 days, and nothing else; A2
   the agent at L1 and A3 at L2 with every simulated customer allowlisted, both with the rule
   planner and templates (no LLM in the simulation, so a run is reproducible).
4. What was sent, escalated, resolved or allocated that day is told to the customers.

Metrics are taken as of the last day. The results JSON holds no run ids, uuids or wall-clock
times, so the same seed gives byte-identical results (T-SIM-01).
"""

import csv
import dataclasses
import io
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.adapters.config import Config
from receivables.adapters.db.agent_store import DbAgentStore, run_agent_tick, tick_config
from receivables.adapters.db.allocations import allocate_payment
from receivables.adapters.db.classifications import accept, classify_stored
from receivables.adapters.db.drafts import (
    approve_draft,
    create_draft,
    mark_draft_sent,
    reject_draft,
)
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.models import (
    AgentStep,
    Approval,
    Classification,
    Correspondence,
    Draft,
    Event,
    MailOutbox,
)
from receivables.adapters.db.repo import (
    load_agent_facts,
    load_invoices,
    load_payments,
    write_audit,
)
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.agent.orchestrator import recheck_draft
from receivables.agent.policy import SendDraft, evaluate_send, send_facts_from
from receivables.core.ledger import build_ledger
from receivables.core.money import from_fils
from receivables.core.scoring import round_half_up
from receivables.sim.behaviour import CustomerBehaviour, CustomerPayment, CustomerReply
from receivables.sim.behaviour import behaviours_for
from receivables.sim.company import SimCompany
from receivables.sim.scenario import Scenario
from receivables.sim.simulator import (
    LABEL,
    STAGING_SUFFIX,
    SimRefused,
    check_destination,
    create_sim_db,
)

__all__ = ["ARMS", "HUMAN", "SimRun", "results_path", "run_arms"]

ARMS = ("A0", "A1", "A2", "A3")
HUMAN = "sim-human"
RULES = "sim-rules"
CUSTOMER_TIME = time(9, 0)
HUMAN_TIME = time(9, 30)
ARM_TIME = time(10, 0)
REMINDER_EVERY_DAYS = 7  # A1
RESOLVE_AFTER_WORKING_DAYS = 3  # option 3a [A]
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DELIVERED = ("delivered_sandbox", "sent")


@dataclass(frozen=True, slots=True)
class SimRun:
    results: Mapping[str, Any]
    results_path: Path
    databases: Mapping[str, Path]


def results_path(data_dir: Path, seed: int) -> Path:
    return data_dir / f"sim_{seed}_results.json"


def _suffix(invoice_number: str) -> str:
    return invoice_number.rsplit("-", 1)[1]


def _csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def _local_date(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(DUBAI).date()


@dataclass
class _Arm:
    name: str
    scenario: Scenario
    company: SimCompany
    engine: Engine
    clock: SimulatedClock
    config: Config
    behaviours: Mapping[str, CustomerBehaviour]
    client: RedactingClient
    truth: dict[str, CustomerReply] = field(default_factory=dict)  # message id -> the reply
    claimed_on: dict[str, date] = field(default_factory=dict)  # invoice -> ALREADY_PAID accepted
    document_requested: dict[str, date] = field(default_factory=dict)  # first, per dispute
    resolved: set[str] = field(default_factory=set)
    last_sent: dict[str, date] = field(default_factory=dict)  # invoice -> last message day

    @property
    def agent(self) -> bool:
        return self.name in ("A2", "A3")

    def at(self, day: date, moment: time) -> None:
        self.clock.set(datetime.combine(day, moment, DUBAI))

    def working(self, day: date) -> bool:
        return _WEEKDAYS[day.weekday()] in self.scenario.working_days

    def working_days_between(self, first: date, last: date) -> int:
        """Working days after ``first``, up to and including ``last``."""
        return sum(
            self.working(first + timedelta(days=n)) for n in range(1, (last - first).days + 1)
        )

    def _behaviour(self, invoice_number: str) -> CustomerBehaviour:
        customer = next(i.customer_id for i in self.company.invoices
                        if i.invoice_number == invoice_number)  # fmt: skip
        return self.behaviours[customer]

    def _sent(self, invoice_numbers: Iterable[str], day: date) -> None:
        for invoice in invoice_numbers:
            self.last_sent[invoice] = day
            self._behaviour(invoice).message_sent(invoice, day)

    # 1. The customers -------------------------------------------------------------------------

    def customers(self, day: date) -> None:
        self.at(day, CUSTOMER_TIME)
        actions = [a for b in self.behaviours.values() for a in b.actions_on(day)]
        payments = [a for a in actions if isinstance(a, CustomerPayment)]
        replies = [a for a in actions if isinstance(a, CustomerReply)]
        if payments:
            self._import("payments", _csv(
                ("payment_id", "invoice_number", "customer_id", "payment_date", "amount",
                 "payment_type"),
                ((p.payment_id, p.invoice_number, p.customer_id, p.day.isoformat(),
                  from_fils(p.amount_fils), "payment") for p in payments),
            ), day)  # fmt: skip
        if replies:
            sent_at = datetime.combine(day, CUSTOMER_TIME, DUBAI).isoformat()
            self._import("correspondence", _csv(
                ("message_id", "direction", "sent_at", "invoice_number", "customer_id",
                 "language", "subject", "body"),
                ((r.message_id, "inbound", sent_at, r.invoice_number, r.customer_id, "en",
                  f"Re: invoice {r.invoice_number}", r.text) for r in replies),
            ), day)  # fmt: skip
            for reply in replies:
                self.truth[reply.message_id] = reply
                if self.agent:
                    classify_stored(
                        self.engine, reply.message_id, client=self.client,
                        review_threshold=self.config.llm.review_confidence_threshold,
                        clock=self.clock, actor="inbox",
                    )  # fmt: skip

    def _import(self, kind: str, raw: bytes, day: date) -> None:
        result = import_csv(
            self.engine, kind, raw, filename=f"sim_{kind}_{day.isoformat()}.csv",
            clock=self.clock, actor="simulator",
        )  # fmt: skip
        if result.status != "accepted":
            codes = [(e.line, e.code) for e in result.errors]
            raise SimRefused(f"simulated {kind} on {day} were not accepted: {codes}")

    # 2. The simulated human ---------------------------------------------------------------------

    def human(self, day: date) -> None:
        if self.name == "A0":
            return
        self.at(day, HUMAN_TIME)
        if self.agent:
            self._review_inbox(day)
            self._allocate(day)
            self._resolve(day)
        self._send_drafts(day)

    def _review_inbox(self, day: date) -> None:
        with self.engine.connect() as conn:
            pending = conn.execute(
                select(Classification.message_id, Correspondence.sent_at)
                .join(Correspondence, Correspondence.message_id == Classification.message_id)
                .where(Classification.status == "pending")
                .order_by(Correspondence.sent_at, Classification.message_id)
            ).all()
        for message_id, sent_at in pending:
            if _local_date(sent_at) >= day:
                continue
            reply = self.truth[message_id]
            accept(
                self.engine, message_id, label_override=reply.label, promised_payment_date=None,
                clock=self.clock, actor=HUMAN,
            )  # fmt: skip
            if reply.label == "ALREADY_PAID":
                self.claimed_on.setdefault(reply.invoice_number, day)

    def _allocate(self, day: date) -> None:
        cash = {p.customer_id: p.payment_id for p in self.company.payments}
        for invoice, accepted_on in sorted(self.claimed_on.items()):
            if self.working_days_between(accepted_on, day) < 1:
                continue
            del self.claimed_on[invoice]
            behaviour = self._behaviour(invoice)
            allocate_payment(
                self.engine, cash[behaviour.customer.customer_id], invoice,
                clock=self.clock, actor=HUMAN,
            )  # fmt: skip
            behaviour.allocation_confirmed(invoice, day)

    def _resolve(self, day: date) -> None:
        for invoice, requested in sorted(self.document_requested.items()):
            if invoice in self.resolved:
                continue
            if self.working_days_between(requested, day) < RESOLVE_AFTER_WORKING_DAYS:
                continue
            self.resolved.add(invoice)
            self._import("events", _csv(
                ("event_id", "invoice_number", "event_type", "event_date", "label",
                 "promise_date"),
                [(f"SIM-X{_suffix(invoice)}", invoice, "DISPUTE_RESOLVED", day.isoformat(), "",
                  "")],
            ), day)  # fmt: skip
            self._behaviour(invoice).dispute_resolved(invoice, day)

    def _send_drafts(self, day: date) -> None:
        with self.engine.connect() as conn:
            waiting = [
                d.draft_id
                for d in conn.execute(
                    select(Draft.draft_id, Draft.created_at)
                    .where(Draft.status == "draft")
                    .order_by(Draft.created_at, Draft.draft_id)
                ).all()
                if _local_date(d.created_at) < day
            ]
        gap = self.config.agent.l2.min_days_between_contacts
        for draft_id in waiting:
            with self.engine.connect() as conn, conn.begin():
                store = DbAgentStore(conn, self.clock)
                draft = store.get_draft(draft_id)
                assert draft is not None  # noqa: S101 - read from the table just above
                facts = load_agent_facts(conn, day, self.config.engine)
                recent = [n for n in draft.invoice_numbers
                          if n in self.last_sent and (day - self.last_sent[n]).days < gap]  # fmt: skip
                stale = recheck_draft(facts, draft, tick_config(self.config))
                if not recent and not stale:
                    self._record_policy(conn, store, facts, draft)
            if recent or stale:
                reason = "contacted recently" if recent else "out of date: " + ", ".join(stale)
                reject_draft(self.engine, draft_id, reason, clock=self.clock, actor=HUMAN)
                continue
            approve_draft(self.engine, draft_id, clock=self.clock, actor=HUMAN)
            mark_draft_sent(self.engine, draft_id, self.clock.now(), clock=self.clock, actor=HUMAN)
            self._sent(draft.invoice_numbers, day)

    def _record_policy(self, conn: Any, store: DbAgentStore, facts: Any, draft: Any) -> None:
        """The policy engine's verdict on a message a person sends (T-SIM-03): recorded, and the
        person sends it anyway, as at L1 (D-046)."""
        l2 = self.config.agent.l2
        customer = facts.customers[draft.customer_id]
        decision = evaluate_send(
            send_facts_from(
                facts, draft.invoice_numbers, timezone=l2.timezone,
                sends_today=store.sends_on(self.clock.today(), l2.timezone),
                outbound_dates=store.outbound_dates(draft.customer_id, l2.timezone),
            ),
            SendDraft(invoice_numbers=draft.invoice_numbers, tone=draft.tone,
                      recipient=customer.contact_email or "", violations=draft.violations),
            self.clock.now(), l2,
        )  # fmt: skip
        write_audit(
            conn, self.clock, actor=HUMAN, action="sim.policy_checked", entity_type="draft",
            entity_id=draft.draft_id, before=None,
            after={"allowed": decision.allowed, "reasons": list(decision.reasons),
                   "violations": list(draft.violations)},
        )  # fmt: skip

    # 3. The arm ---------------------------------------------------------------------------------

    def act(self, day: date) -> None:
        self.at(day, ARM_TIME)
        if self.name == "A1":
            self._rules_reminders(day)
        elif self.agent:
            self._agent_tick(day)

    def _rules_reminders(self, day: date) -> None:
        with self.engine.connect() as conn:
            rows = build_ledger(load_invoices(conn), load_payments(conn), day)
            pending = {
                n
                for (numbers,) in conn.execute(
                    select(Draft.invoice_numbers_json).where(Draft.status == "draft")
                ).all()
                for n in json.loads(numbers)
            }
        for row in rows:
            if row.open_amount <= 0 or (row.days_overdue or 0) <= 0:
                continue
            last = self.last_sent.get(row.invoice_number)
            if row.invoice_number in pending or (
                last is not None and (day - last).days < REMINDER_EVERY_DAYS
            ):
                continue
            create_draft(
                self.engine, [row.invoice_number], None, config=self.config, clock=self.clock,
                actor=RULES, drafter=None,
            )  # fmt: skip

    def _agent_tick(self, day: date) -> None:
        summary = run_agent_tick(
            self.engine, self.clock, self.config, as_of=day,
            autonomy_level=1 if self.name == "A2" else 2, planner=None, drafter=None,
        )  # fmt: skip
        with self.engine.connect() as conn:
            steps = conn.execute(
                select(AgentStep.invoice_number, AgentStep.tool, AgentStep.result_json)
                .where(AgentStep.run_id == summary.run_id, AgentStep.status == "executed")
                .order_by(AgentStep.step_id)
            ).all()
            for invoice, tool, result_json in steps:
                if tool == "send_email":  # delivered to the sandbox at L2
                    draft_id = json.loads(result_json)["draft_id"]
                    numbers = conn.execute(
                        select(Draft.invoice_numbers_json).where(Draft.draft_id == draft_id)
                    ).scalar_one()
                    self._sent(json.loads(numbers), day)
                elif tool == "request_internal_document" and invoice not in self.resolved:
                    self.document_requested.setdefault(invoice, day)
                elif tool == "escalate":
                    self._behaviour(invoice).escalated(invoice, day)

    # Metrics ----------------------------------------------------------------------------------

    def metrics(self, as_of: date) -> dict[str, Any]:
        with self.engine.connect() as conn:
            rows = build_ledger(load_invoices(conn), load_payments(conn), as_of)
            events = conn.execute(
                select(Event.invoice_number, Event.event_type, Event.event_date).order_by(
                    Event.invoice_number, Event.event_date, Event.event_id
                )
            ).all()
            manual = conn.execute(
                select(func.count()).select_from(Draft).where(Draft.status == "sent_manually")
            ).scalar_one()
            sandbox = conn.execute(
                select(func.count()).select_from(MailOutbox).where(
                    MailOutbox.status.in_(_DELIVERED)
                )
            ).scalar_one()
            approvals = conn.execute(select(func.count()).select_from(Approval)).scalar_one()
            denials = conn.execute(
                select(func.count()).select_from(AgentStep).where(
                    AgentStep.tool == "send_email", AgentStep.policy_allowed == 0
                )
            ).scalar_one()
        overdue = sum(r.open_amount for r in rows if r.open_amount > 0 and (r.days_overdue or 0) > 0)
        late = [(r.settled_date - r.due_date).days for r in rows if r.settled_date is not None]
        opened: dict[str, date] = {}
        cycles: list[int] = []
        for invoice, kind, event_date in events:
            when = date.fromisoformat(event_date)
            if kind == "DISPUTE_OPENED":
                opened.setdefault(invoice, when)
            elif kind == "DISPUTE_RESOLVED" and invoice in opened:
                cycles.append((when - opened.pop(invoice)).days)
        return {
            "overdue_amount": str(from_fils(overdue)),
            "mean_days_past_due_at_payment": _mean(late),
            "invoices_settled": len(late),
            "mean_dispute_cycle_days": _mean(cycles),
            "disputes_resolved": len(cycles),
            "disputes_open": len(opened),
            "messages_sent": manual + sandbox,
            "approvals_needed": approvals,
            "policy_denials": denials,
        }


def _mean(values: Sequence[int]) -> str | None:
    return None if not values else str(round_half_up(Fraction(sum(values), len(values)), 2))


def _arm_config(config: Config, arm: str, company: SimCompany) -> Config:
    if arm != "A3":
        return config
    l2 = dataclasses.replace(
        config.agent.l2, allowlisted_customers=tuple(c.customer_id for c in company.customers)
    )
    return dataclasses.replace(config, agent=dataclasses.replace(config.agent, l2=l2))


def _run_arm(
    arm: str, scenario: Scenario, data_dir: Path, real_database_url: str, config: Config
) -> tuple[Path, dict[str, Any], SimCompany]:
    created = create_sim_db(scenario, data_dir, real_database_url, arm=arm)
    company = created.company
    engine = make_engine(f"sqlite:///{created.path}")
    names = [c.customer_name for c in company.customers]
    state = _Arm(
        name=arm,
        scenario=scenario,
        company=company,
        engine=engine,
        clock=SimulatedClock(datetime.combine(scenario.start_date, time(0, 0), DUBAI)),
        config=_arm_config(config, arm, company),
        behaviours=behaviours_for(company),
        client=RedactingClient(
            FakeClient(),  # no model in the simulation: every reply falls back to review
            redact_before_send=config.llm.redact_before_send,
            pseudonymize_customer_names=config.llm.pseudonymize_customer_names,
            customer_names=lambda: names,
        ),
    )
    try:
        last = scenario.start_date
        for offset in range(scenario.days + 1):
            day = scenario.start_date + timedelta(days=offset)
            state.customers(day)
            if state.working(day):
                state.human(day)
                state.act(day)
            last = day
        return created.path, state.metrics(last), company
    finally:
        engine.dispose()


def run_arms(
    scenario: Scenario, data_dir: Path, real_database_url: str, config: Config
) -> SimRun:
    """Run A0 to A3 on the seeded company and write ``sim_<seed>_results.json``."""
    out = results_path(data_dir, scenario.seed)
    staging = Path(f"{out}{STAGING_SUFFIX}")
    check_destination(out, staging, real_database_url, sqlite=False)
    databases: dict[str, Path] = {}
    arms: dict[str, Any] = {}
    company: SimCompany | None = None
    for arm in ARMS:
        path, metrics, company = _run_arm(arm, scenario, data_dir, real_database_url, config)
        databases[arm] = path
        arms[arm] = metrics
    assert company is not None  # noqa: S101 - ARMS is not empty
    end = scenario.start_date + timedelta(days=scenario.days)
    results = {
        "label": LABEL,
        "scenario": scenario.name,
        "seed": scenario.seed,
        "start_date": scenario.start_date.isoformat(),
        "metrics_as_of": end.isoformat(),
        "company_sha256": company.sha256(),
        "assumptions": {
            "planner": "rules (no LLM in the simulation)",
            "classifier": "fake: every reply goes to review",
            "human_acts_at": HUMAN_TIME.isoformat("minutes"),
            "human_waits_working_days": 1,
            "dispute_resolved_after_working_days": RESOLVE_AFTER_WORKING_DAYS,
            "a1_reminder_every_days": REMINDER_EVERY_DAYS,
            "a3_allowlist": "every simulated customer",
        },
        "arms": arms,
    }
    text = json.dumps(results, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        staging.write_text(text, encoding="utf-8")
        check_destination(out, staging, real_database_url, sqlite=False)
        os.replace(staging, out)
    finally:
        staging.unlink(missing_ok=True)
    return SimRun(results=results, results_path=out, databases=databases)
