"""SQLite implementation of the orchestrator's ``AgentStore`` port (SPEC §18.6, §18.10; D-024).

All writes happen on the caller's connection, inside one transaction per tick; each case runs
in a savepoint so a failing case is rolled back alone. Timestamps come from the injected clock.
Run starts and finishes, case state changes and plan rejections are audited here; per-step
audit rows are S2.08.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, insert, select, update

from receivables.adapters.clock import Clock, utc_timestamp
from receivables.adapters.config import Config
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
    Task,
)
from receivables.adapters.db.repo import load_agent_facts, write_audit
from receivables.agent.cases import CaseState
from receivables.agent.facts import AgentFacts
from receivables.agent.orchestrator import (
    DraftRecord,
    PlanSource,
    RunSummary,
    StepRecord,
    TickConfig,
    run_tick,
)
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
            actor=ACTOR,
            action="case.transition",
            entity_type="case",
            entity_id=invoice_number,
            before=None if before is None else {"state": before.value},
            after={
                "state": after.value,
                "trigger": trigger,
                "run_id": run_id,
                "event_id": event_id,
            },
        )

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

    def add_sandbox_mail(self, mail_id: str, *, fields: Mapping[str, Any]) -> None:
        """Sandbox only: a row in ``mail_outbox``; no network connection is ever opened."""
        draft = self.conn.execute(select(Draft).where(Draft.draft_id == fields["draft_id"])).one()
        self.conn.execute(
            insert(MailOutbox).values(
                mail_id=mail_id,
                draft_id=draft.draft_id,
                to_address=fields["to"],
                subject=draft.subject,
                body=draft.body,
                gateway="sandbox",
                status="delivered_sandbox",
                created_at=_utc(fields["at"]),  # the tick's now_local, as the daily cap counts
            )
        )

    def sends_on(self, local_date: date, timezone: str) -> int:
        stamps = self.conn.execute(
            select(MailOutbox.created_at).where(MailOutbox.status.in_(_DELIVERED))
        ).scalars()
        return sum(1 for s in stamps if _local_date(s, timezone) == local_date)

    def outbound_dates(self, customer_id: str, timezone: str) -> tuple[date, ...]:
        """Every earlier outbound contact, up to now: sandbox sends and outbound messages."""
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
        dates = {_local_date(s, timezone) for s in [*sent, *written]}
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
    )


def run_agent_tick(
    engine: Engine,
    clock: Clock,
    config: Config,
    *,
    as_of: date | None = None,
    autonomy_level: int | None = None,
    planner: PlanSource | None = None,
) -> RunSummary:
    """One tick in one transaction; ``now_local`` and the default ``as_of`` from the clock."""
    now_local = clock.now()
    with engine.begin() as conn:
        return run_tick(
            DbAgentStore(conn, clock),
            as_of=as_of or clock.today(),
            now_local=now_local,
            autonomy_level=config.agent.autonomy_level
            if autonomy_level is None
            else autonomy_level,
            config=tick_config(config),
            planner=planner,
        )
