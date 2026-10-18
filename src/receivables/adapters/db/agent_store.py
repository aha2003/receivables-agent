"""SQLite implementation of the orchestrator's ``AgentStore`` port (SPEC §18.6, §18.10; D-024).

All writes happen on the caller's connection, inside one transaction per tick; each case runs
in a savepoint so a failing case is rolled back alone. Timestamps come from the injected clock.
Run starts and finishes, every step, case state changes and plan rejections are audited here.
"""

import dataclasses
import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, insert, select, update

from receivables.adapters.clock import DUBAI, Clock, utc_timestamp
from receivables.adapters.config import Config
from receivables.adapters.db.controls import kill_switch_on
from receivables.adapters.db.models import (
    AgentRun,
    AgentStep,
    Approval,
    AuditLog,
    Case,
    Correspondence,
    Draft,
    Invoice,
    MailOutbox,
    Outcome,
    Task,
)
from receivables.adapters.db.repo import load_agent_facts, write_audit
from receivables.adapters.mail.sandbox import SandboxMailGateway
from receivables.agent.cases import CaseState, CaseTrigger, MessageSent, transition
from receivables.agent.drafting import DraftWriter
from receivables.agent.facts import AgentFacts
from receivables.agent.orchestrator import (
    DraftRecord,
    PlanSource,
    RunSummary,
    StepRecord,
    TickConfig,
    run_tick,
)
from receivables.agent.outcomes import InferredOutcome
from receivables.core.config import EngineConfig

__all__ = ["ACTOR", "DbAgentStore", "run_agent_tick", "tick_config"]

ACTOR = "agent"
_DELIVERED = ("delivered_sandbox", "sent")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date(timestamp: str, timezone: str) -> date | None:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(ZoneInfo(timezone)).date()


class DbAgentStore:
    def __init__(self, conn: Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def _now(self) -> str:
        return utc_timestamp(self.clock)

    def audit(self, action: str, entity_type: str, entity_id: str, after: Any) -> None:
        write_audit(
            self.conn,
            self.clock,
            actor=ACTOR,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=None,
            after=after,
        )

    # --- outcomes (§18.7) ------------------------------------------------------------------

    def outcome_ids(self) -> set[str]:
        return set(self.conn.execute(select(Outcome.outcome_id)).scalars())

    def add_outcome(self, outcome: InferredOutcome, run_id: str) -> None:
        self.conn.execute(
            insert(Outcome).values(
                outcome_id=outcome.outcome_id,
                invoice_number=outcome.invoice_number,
                recorded_at=utc_timestamp(self.clock),
                outcome_type=outcome.outcome_type,
                note=outcome.note,
            )
        )
        self.audit(
            "outcome.recorded",
            "invoice",
            outcome.invoice_number,
            {"outcome_id": outcome.outcome_id, "outcome_type": outcome.outcome_type,
             "run_id": run_id},
        )  # fmt: skip

    # --- facts and cases -----------------------------------------------------------------

    def load_facts(self, as_of: date, config: EngineConfig) -> AgentFacts:
        return load_agent_facts(self.conn, as_of, config)

    def case_states(self) -> Mapping[str, CaseState]:
        rows = self.conn.execute(select(Case.invoice_number, Case.state)).all()
        return {r.invoice_number: CaseState(r.state) for r in rows}

    def save_case(
        self,
        invoice_number: str,
        before: CaseState | None,
        after: CaseState,
        trigger: str,
        run_id: str,
        event_id: str | None = None,
    ) -> None:
        self._store_case(
            invoice_number, before, after, actor=ACTOR,
            after_json={"trigger": trigger, "run_id": run_id, "event_id": event_id},
        )  # fmt: skip

    def _store_case(
        self,
        invoice_number: str,
        before: CaseState | None,
        after: CaseState,
        *,
        actor: str,
        after_json: Mapping[str, Any],
    ) -> None:
        now = self._now()
        if before is None:
            self.conn.execute(
                insert(Case).values(invoice_number=invoice_number, state=after, updated_at=now)
            )
        else:
            self.conn.execute(
                update(Case)
                .where(Case.invoice_number == invoice_number)
                .values(state=after, updated_at=now)
            )
        write_audit(
            self.conn,
            self.clock,
            actor=actor,
            action="case.transition",
            entity_type="case",
            entity_id=invoice_number,
            before=None if before is None else {"state": before.value},
            after={"state": after.value, **after_json},
        )

    def message_sent(self, invoice_numbers: Iterable[str], *, actor: str, draft_id: str) -> None:
        """§18.2 for a person's send (marked sent, or an L2 override): ``ASSESSED`` cases of
        the draft's invoices move to ``AWAITING_REPLY``; other states are unchanged."""
        for invoice in invoice_numbers:
            self.apply_by_person(
                invoice, MessageSent(), actor=actor, context={"draft_id": draft_id}
            )

    def apply_by_person(
        self, invoice: str, trigger: CaseTrigger, *, actor: str, context: Mapping[str, Any]
    ) -> None:
        """One §18.2 trigger caused by a person's action, outside a tick; an invoice without a
        case, or a row that does not match, is left unchanged."""
        before = self.case_states().get(invoice)
        result = transition(before, trigger)
        if result.applied and result.to_state is not None and result.to_state != before:
            self._store_case(
                invoice, before, result.to_state, actor=actor,
                after_json={"trigger": type(trigger).__name__, **context},
            )  # fmt: skip

    def mark_event(self, invoice_number: str, event_id: str, trigger: str, run_id: str) -> None:
        """Record that an event was applied without changing the state (it is not re-applied)."""
        self.audit(
            "case.event_applied",
            "case",
            invoice_number,
            {"trigger": trigger, "run_id": run_id, "event_id": event_id},
        )

    def applied_events(self) -> set[tuple[str, str]]:
        """(invoice, event id) pairs already applied to cases, from the audit log."""
        rows = self.conn.execute(
            select(AuditLog.entity_id, AuditLog.after_json).where(
                AuditLog.entity_type == "case",
                AuditLog.action.in_(("case.transition", "case.event_applied")),
            )
        ).all()
        applied: set[tuple[str, str]] = set()
        for row in rows:
            event_id = json.loads(row.after_json or "{}").get("event_id")
            if event_id is not None:
                applied.add((row.entity_id, event_id))
        return applied

    def set_followup(self, invoice_number: str, on: date) -> None:
        self.conn.execute(
            update(Case)
            .where(Case.invoice_number == invoice_number)
            .values(next_followup=on.isoformat(), updated_at=self._now())
        )

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.conn.begin_nested():
            yield

    # --- runs and steps --------------------------------------------------------------------

    def start_run(self, summary: RunSummary, now: datetime) -> None:
        started = self._now()
        self.conn.execute(
            insert(AgentRun).values(
                run_id=summary.run_id,
                as_of=summary.as_of.isoformat(),
                now_ts=_utc(now),
                autonomy_level=summary.autonomy_level,
                planner=summary.planner,
                fallback=0,
                model=summary.model,
                started_at=started,
            )
        )
        self.audit(
            "agent.run_started",
            "agent_run",
            summary.run_id,
            {"as_of": summary.as_of.isoformat(), "autonomy_level": summary.autonomy_level,
             "planner": summary.planner},
        )  # fmt: skip

    def finish_run(self, summary: RunSummary) -> None:
        self.conn.execute(
            update(AgentRun)
            .where(AgentRun.run_id == summary.run_id)
            .values(
                fallback=int(summary.fallback),
                cases_considered=summary.cases_considered,
                steps_executed=summary.steps_executed,
                steps_denied=summary.steps_denied,
                finished_at=self._now(),
            )
        )
        self.audit(
            "agent.run_finished",
            "agent_run",
            summary.run_id,
            {
                "fallback": summary.fallback,
                "cases_considered": summary.cases_considered,
                "steps_executed": summary.steps_executed,
                "steps_denied": summary.steps_denied,
                "case_errors": summary.case_errors,
            },
        )

    def add_step(self, step: StepRecord) -> None:
        self.conn.execute(
            insert(AgentStep).values(
                step_id=step.step_id,
                run_id=step.run_id,
                invoice_number=step.invoice_number,
                seq=step.seq,
                tool=step.tool,
                args_json=_json(step.args),
                reason=step.reason,
                policy_allowed=None if step.policy_allowed is None else int(step.policy_allowed),
                policy_reasons_json=_json(list(step.policy_reasons)),
                status=step.status,
                result_json=None if step.result is None else _json(step.result),
            )
        )
        # Every step is audited, executed or not (§18.5); arguments and outputs stay in the trace.
        write_audit(
            self.conn,
            self.clock,
            actor=ACTOR,
            action="agent.step",
            entity_type="agent_step",
            entity_id=step.step_id,
            before={"case_state": step.state_before},
            after={
                "run_id": step.run_id,
                "invoice_number": step.invoice_number,
                "tool": step.tool,
                "status": step.status,
                "policy_allowed": step.policy_allowed,
                "policy_reasons": list(step.policy_reasons),
                "case_state": step.state_after,
            },
        )

    # --- drafts, tasks, approvals, sandbox outbox ----------------------------------------------

    def add_draft(self, draft_id: str, *, fields: Mapping[str, Any]) -> None:
        self.conn.execute(
            insert(Draft).values(
                draft_id=draft_id,
                invoice_numbers_json=_json(fields["invoice_numbers"]),
                customer_id=fields["customer_id"],
                tone=fields["tone"],
                subject=fields["subject"],
                body=fields["body"],
                facts_json=_json(fields["facts"]),
                generator=fields["generator"],
                violations_json=_json(fields["violations"]),
                status="draft",
                created_at=self._now(),
            )
        )

    def get_draft(self, draft_id: str) -> DraftRecord | None:
        draft = self.conn.execute(select(Draft).where(Draft.draft_id == draft_id)).first()
        if draft is None:
            return None
        return DraftRecord(
            draft_id=draft.draft_id,
            customer_id=draft.customer_id,
            invoice_numbers=tuple(json.loads(draft.invoice_numbers_json)),
            tone=draft.tone,
            dispute_label=json.loads(draft.facts_json).get("dispute_label"),
            subject=draft.subject,
            body=draft.body,
            status=draft.status,
            violations=tuple(json.loads(draft.violations_json)),
        )

    def add_task(self, task_id: str, *, fields: Mapping[str, Any]) -> None:
        self.conn.execute(insert(Task).values(task_id=task_id, **fields))

    def add_approval(self, approval_id: str, *, step_id: str, kind: str, payload: Any) -> None:
        self.conn.execute(
            insert(Approval).values(
                approval_id=approval_id, step_id=step_id, kind=kind, payload_json=_json(payload)
            )
        )

    def deliver_draft(self, mail_id: str, *, draft_id: str, to: str, at: datetime) -> bool:
        """An allowed L2 send through the sandbox gateway (§18.8): a ``mail_outbox`` row, no
        network. Delivered drafts become ``approved`` by ``agent`` (D-038); ``at`` is the
        tick's now_local, as the daily cap counts."""
        result = SandboxMailGateway(self.conn).send(
            mail_id=mail_id, draft_id=draft_id, to=to, at=at
        )
        if result.delivered:
            changed = self.conn.execute(
                update(Draft)
                .where(Draft.draft_id == draft_id, Draft.status == "draft")
                .values(status="approved", approved_by=ACTOR, approved_at=_utc(at))
            ).rowcount
            if changed:
                write_audit(
                    self.conn, self.clock, actor=ACTOR, action="draft.approved",
                    entity_type="draft", entity_id=draft_id, before={"status": "draft"},
                    after={"status": "approved", "approved_by": ACTOR, "mail_id": mail_id},
                )  # fmt: skip
        return result.delivered

    def sends_on(self, local_date: date, timezone: str) -> int:
        stamps = self.conn.execute(
            select(MailOutbox.created_at).where(MailOutbox.status.in_(_DELIVERED))
        ).scalars()
        return sum(1 for s in stamps if _local_date(s, timezone) == local_date)

    def outbound_dates(self, customer_id: str, timezone: str) -> tuple[date, ...]:
        """Every earlier outbound contact, up to now: sandbox sends, outbound messages and drafts
        a person marked sent from their own email (S3.09)."""
        sent = self.conn.execute(
            select(MailOutbox.created_at)
            .join(Draft, Draft.draft_id == MailOutbox.draft_id)
            .where(MailOutbox.status.in_(_DELIVERED), Draft.customer_id == customer_id)
        ).scalars()
        invoice_customer = (
            select(Invoice.customer_id)
            .where(Invoice.invoice_number == Correspondence.invoice_number)
            .scalar_subquery()
        )
        written = self.conn.execute(
            select(Correspondence.sent_at).where(
                Correspondence.direction == "outbound",
                (Correspondence.customer_id == customer_id)
                | (Correspondence.customer_id.is_(None) & (invoice_customer == customer_id)),
            )
        ).scalars()
        by_hand = self.conn.execute(
            select(Draft.sent_at).where(
                Draft.status == "sent_manually",
                Draft.customer_id == customer_id,
                Draft.sent_at.is_not(None),
            )
        ).scalars()
        dates = {_local_date(s, timezone) for s in [*sent, *written, *by_hand] if s}
        return tuple(sorted(d for d in dates if d is not None))


def tick_config(config: Config) -> TickConfig:
    return TickConfig(
        engine=config.engine,
        l2=config.agent.l2,
        max_cases_per_tick=config.agent.max_cases_per_tick,
        max_steps_per_case=config.agent.max_steps_per_case,
        max_words=config.drafts.max_words,
        invoice_regex=config.drafts.invoice_number_regex,
        sender_name=config.drafts.sender_name,
        max_regenerations=config.drafts.max_regenerations,
        sender_domain=config.drafts.sender_domain,
        promise_grace_days=config.agent.promise_grace_days,
    )


def run_agent_tick(
    engine: Engine,
    clock: Clock,
    config: Config,
    *,
    as_of: date | None = None,
    autonomy_level: int | None = None,
    planner: PlanSource | None = None,
    drafter: DraftWriter | None = None,
    now_local: datetime | None = None,
    dry_run: bool = False,
    requested_by: str | None = None,
    request: Mapping[str, Any] | None = None,
) -> RunSummary:
    """One tick in one transaction; ``dry_run`` rolls the whole tick back, trace included.

    ``drafter`` writes LLM drafts in real runs; a dry run uses the template (D-036).
    ``now_local`` defaults to the clock (callers decide when another time is allowed, D-026);
    ``as_of`` defaults to that time's date in Dubai. The effective kill switch (config or
    runtime, D-025) is read inside the tick's transaction.
    """
    now = now_local or clock.now()
    level = config.agent.autonomy_level if autonomy_level is None else autonomy_level
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            l2 = dataclasses.replace(config.agent.l2, kill_switch=kill_switch_on(conn, config))
            summary = run_tick(
                DbAgentStore(conn, clock),
                as_of=as_of or now.astimezone(DUBAI).date(),
                today=clock.today(),  # the real date, whatever ``now`` was requested (D-051)
                now_local=now,
                autonomy_level=level,
                config=dataclasses.replace(tick_config(config), l2=l2),
                # A dry run plans by rules and drafts from the templates: a preview never spends
                # model calls (D-036, D-037).
                planner=None if dry_run else planner,
                drafter=None if dry_run else drafter,
            )
            if requested_by is not None:  # who started the run, with the values they chose
                write_audit(
                    conn,
                    clock,
                    actor=requested_by,
                    action="agent.run_requested",
                    entity_type="agent_run",
                    entity_id=summary.run_id,
                    before=None,
                    after={"run_id": summary.run_id, "request": dict(request or {})},
                )
        except BaseException:
            transaction.rollback()
            raise
        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
        return summary
