"""Read models for agent runs, approvals and one case's trace (SPEC §18.11, §10; D-026, D-027).

Shared by the JSON API and the server-rendered pages, so both show the same trace.
"""

import json
from typing import Any

from sqlalchemy import Engine, select, text

from receivables.adapters.db.models import Customer
from receivables.adapters.db.repo import load_playbooks
from receivables.agent.facts import PlaybookRecord
from receivables.agent.playbook import allowlist_suggestion, build_playbook

__all__ = ["case_runs", "customer_playbook", "pending_approvals", "run_trace", "send_decisions"]


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def run_trace(engine: Engine, run_id: str) -> dict[str, Any] | None:
    """One run: the run row, its steps grouped per case (the plan), and its audit events."""
    with engine.connect() as conn:
        run = (
            conn.execute(text("SELECT * FROM agent_runs WHERE run_id = :r"), {"r": run_id})
            .mappings()
            .first()
        )
        if run is None:
            return None
        steps = (
            conn.execute(
                text("SELECT * FROM agent_steps WHERE run_id = :r ORDER BY seq"), {"r": run_id}
            )
            .mappings()
            .all()
        )
        events = (
            conn.execute(
                text(
                    "SELECT audit_id, ts, actor, action, entity_type, entity_id, after_json"
                    " FROM audit_log WHERE action <> 'agent.step'"
                    " AND (json_extract(after_json, '$.run_id') = :r"
                    "      OR (entity_type = 'agent_run' AND entity_id = :r))"
                    " ORDER BY audit_id"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
    cases: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        cases.setdefault(s["invoice_number"], []).append(_step(s))
    return {
        "run": {
            "run_id": run["run_id"],
            "as_of": run["as_of"],
            "now": run["now_ts"],
            "autonomy_level": run["autonomy_level"],
            "planner": run["planner"],
            "fallback": bool(run["fallback"]),
            "model": run["model"],
            "cases_considered": run["cases_considered"],
            "steps_executed": run["steps_executed"],
            "steps_denied": run["steps_denied"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "cases": [{"invoice_number": n, "steps": steps} for n, steps in cases.items()],
        "events": [
            {
                "audit_id": e["audit_id"],
                "ts": e["ts"],
                "action": e["action"],
                "entity_type": e["entity_type"],
                "entity_id": e["entity_id"],
                "detail": _loads(e["after_json"]),
            }
            for e in events
        ],
    }


def pending_approvals(engine: Engine) -> list[dict[str, Any]]:
    """Pending approval items with their reason codes and the draft subject."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT a.approval_id, a.kind, a.step_id, a.payload_json, a.status,"
                    " s.run_id, s.invoice_number, d.subject AS draft_subject"
                    " FROM approvals a JOIN agent_steps s ON s.step_id = a.step_id"
                    " LEFT JOIN drafts d"
                    "   ON d.draft_id = json_extract(a.payload_json, '$.draft_id')"
                    " WHERE a.status = 'pending' ORDER BY a.approval_id"
                )
            )
            .mappings()
            .all()
        )
    items = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        items.append(
            {
                "approval_id": r["approval_id"],
                "kind": r["kind"],
                "status": r["status"],
                "run_id": r["run_id"],
                "step_id": r["step_id"],
                "invoice_number": r["invoice_number"],
                "draft_id": payload.get("draft_id"),
                "draft_subject": r["draft_subject"],
                "payment_id": payload.get("payment_id"),
                "policy_allowed": payload.get("policy_allowed"),
                "reasons": payload.get("reasons", []),
            }
        )
    return items


def _step(s: Any) -> dict[str, Any]:
    return {
        "step_id": s["step_id"],
        "seq": s["seq"],
        "tool": s["tool"],
        "args": _loads(s["args_json"]),
        "reason": s["reason"],
        "policy_allowed": None if s["policy_allowed"] is None else bool(s["policy_allowed"]),
        "policy_reasons": _loads(s["policy_reasons_json"]),
        "status": s["status"],
        "result": _loads(s["result_json"]),
    }


def case_runs(engine: Engine, invoice_number: str, limit: int = 10) -> list[dict[str, Any]]:
    """The most recent runs that planned this invoice, newest first, with this case's steps."""
    with engine.connect() as conn:
        runs = (
            conn.execute(
                text(
                    "SELECT r.* FROM agent_runs r WHERE r.run_id IN"
                    " (SELECT run_id FROM agent_steps WHERE invoice_number = :n)"
                    " ORDER BY r.started_at DESC, r.rowid DESC LIMIT :limit"
                ),
                {"n": invoice_number, "limit": limit},
            )
            .mappings()
            .all()
        )
        steps = (
            conn.execute(
                text(
                    "SELECT * FROM agent_steps WHERE invoice_number = :n"
                    " AND run_id IN (SELECT value FROM json_each(:runs)) ORDER BY seq"
                ),
                {"n": invoice_number, "runs": json.dumps([r["run_id"] for r in runs])},
            )
            .mappings()
            .all()
        )
    by_run: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        by_run.setdefault(s["run_id"], []).append(_step(s))
    return [
        {
            "run_id": r["run_id"],
            "as_of": r["as_of"],
            "now": r["now_ts"],
            "autonomy_level": r["autonomy_level"],
            "planner": r["planner"],
            "fallback": bool(r["fallback"]),
            "model": r["model"],
            "started_at": r["started_at"],
            "steps": by_run.get(r["run_id"], []),
        }
        for r in runs
    ]


def send_decisions(engine: Engine, draft_ids: list[str]) -> dict[str, dict[str, Any]]:
    """For each draft, the most recent ``send_email`` step that used it, with its run's level."""
    if not draft_ids:
        return {}
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT s.*, r.autonomy_level, json_extract(s.result_json, '$.draft_id') AS d"
                    " FROM agent_steps s JOIN agent_runs r ON r.run_id = s.run_id"
                    " WHERE s.tool = 'send_email'"
                    " AND json_extract(s.result_json, '$.draft_id')"
                    "     IN (SELECT value FROM json_each(:ids))"
                    " ORDER BY s.rowid DESC"
                ),
                {"ids": json.dumps(draft_ids)},
            )
            .mappings()
            .all()
        )
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        found.setdefault(r["d"], {**_step(r), "autonomy_level": r["autonomy_level"]})
    return found


def customer_playbook(
    engine: Engine, customer_id: str, allowlisted: tuple[str, ...]
) -> dict[str, Any] | None:
    """§18.11 ``GET /api/agent/playbook/{customer_id}``: the stored row, or the §18.7 defaults
    before any tick has computed one; ``None`` for an unknown customer. The suggestion is
    proposed for a person to apply in config, never applied (§18.7, D-052)."""
    with engine.connect() as conn:
        known = conn.execute(
            select(Customer.customer_id).where(Customer.customer_id == customer_id)
        ).first()
        if known is None:
            return None
        stored = {p.customer_id: p for p in load_playbooks(conn)}.get(customer_id)
    playbook: PlaybookRecord = stored or build_playbook(customer_id, kept=0, broken=0, delays=())
    return {
        "customer_id": customer_id,
        "promise_kept_rate": playbook.promise_kept_rate,
        "promises_counted": playbook.promises_counted,
        "avg_days_to_reply": playbook.avg_days_to_reply,
        "replies_counted": playbook.replies_counted,
        "followup_gap_days": playbook.followup_gap_days,
        "updated_at": playbook.updated_at or None,
        "computed": stored is not None,
        "suggestion": allowlist_suggestion(playbook, allowlisted),
    }
