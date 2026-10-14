"""Runtime controls: the global kill switch (SPEC §18.4, §18.5, §3.3; docs/DECISIONS.md D-025).

The kill switch is on when ``config.yaml`` says so or when an admin turned it on. The runtime
state lives in the append-only audit log: the latest ``kill_switch.set`` row decides, so every
change is audited by construction and cannot be edited afterwards. Changing it is an admin
action that needs a password re-entry within the last 5 minutes (§3.3); refusals are audited
too, in their own transaction, so they are kept even though the call raises.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, select

from receivables.adapters.clock import Clock
from receivables.adapters.config import Config
from receivables.adapters.db.models import AuditLog
from receivables.adapters.db.repo import UserRecord, write_audit

__all__ = [
    "KILL_SWITCH_ACTION",
    "REAUTH_WINDOW",
    "AdminActionRefused",
    "kill_switch_on",
    "runtime_kill_switch",
    "set_kill_switch",
]

KILL_SWITCH_ACTION = "kill_switch.set"
REFUSED_ACTION = "kill_switch.refused"
REAUTH_WINDOW = timedelta(minutes=5)


class AdminActionRefused(PermissionError):
    """The user is not an admin, or has not re-entered their password recently enough."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def runtime_kill_switch(conn: Connection) -> bool:
    """The switch as last set by an admin (off if never set)."""
    latest = conn.execute(
        select(AuditLog.after_json)
        .where(AuditLog.action == KILL_SWITCH_ACTION)
        .order_by(AuditLog.audit_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return bool(json.loads(latest)["on"]) if latest else False


def kill_switch_on(conn: Connection, config: Config) -> bool:
    """Effective switch: on in ``config.yaml`` or turned on at runtime."""
    return config.agent.l2.kill_switch or runtime_kill_switch(conn)


def _refusal(user: UserRecord, now: datetime, reauthenticated_at: datetime | None) -> str | None:
    if user.role != "admin":
        return "NOT_ADMIN"
    if reauthenticated_at is None:
        return "REAUTH_REQUIRED"
    if reauthenticated_at.tzinfo is None or reauthenticated_at > now:
        return "REAUTH_INVALID"
    if now - reauthenticated_at > REAUTH_WINDOW:
        return "REAUTH_REQUIRED"
    return None


def set_kill_switch(
    engine: Engine,
    clock: Clock,
    user: UserRecord,
    *,
    on: bool,
    reauthenticated_at: datetime | None,
) -> bool:
    """Turn the runtime kill switch on or off; returns the new runtime state.

    Raises ``AdminActionRefused`` (after auditing the attempt) unless ``user`` is an admin who
    re-entered their password within ``REAUTH_WINDOW`` of the clock's now.
    """
    refusal = _refusal(user, clock.now(), reauthenticated_at)
    with engine.begin() as conn:
        if refusal is not None:
            write_audit(
                conn,
                clock,
                actor=user.username,
                action=REFUSED_ACTION,
                entity_type="control",
                entity_id="kill_switch",
                before=None,
                after={"requested_on": on, "reason": refusal},
            )
        else:
            before = runtime_kill_switch(conn)
            write_audit(
                conn,
                clock,
                actor=user.username,
                action=KILL_SWITCH_ACTION,
                entity_type="control",
                entity_id="kill_switch",
                before={"on": before},
                after={"on": on},
            )
    if refusal is not None:
        raise AdminActionRefused(refusal)
    return on
