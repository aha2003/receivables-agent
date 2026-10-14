"""Validation of untrusted plans (SPEC §18.6 step 4; docs/DECISIONS.md D-024).

A plan from a planner model is data, never instructions. ``validate_plan`` turns it into a
``Plan`` (every tool in the closed registry, every argument valid, at most the step budget,
every step about this case) or rejects the whole plan with reason codes. A rejection keeps only
the reason codes and a short, sanitised list of the tool names it saw, never the raw text.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from receivables.agent.rules_planner import Plan, PlanStep
from receivables.agent.tools import MAX_PLAN_STEPS, TOOL_REGISTRY

__all__ = ["PLAN_REJECTED", "PlanRejection", "validate_plan"]

PLAN_REJECTED = "PLAN_REJECTED"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_LISTED_TOOLS = 10
_MAX_REASON_CHARS = 200


def _plain(text: str) -> str:
    """The planner's reason as plain text: no control or format characters, bounded."""
    kept = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf"})
    return kept[:_MAX_REASON_CHARS]


@dataclass(frozen=True, slots=True)
class PlanRejection:
    reasons: tuple[str, ...]
    tools: tuple[str, ...]  # sanitised names, for the trace only
    status: str = PLAN_REJECTED


def _tool_names(steps: object) -> tuple[str, ...]:
    if not isinstance(steps, Sequence) or isinstance(steps, str | bytes):
        return ()
    names: list[str] = []
    for step in list(steps)[:_MAX_LISTED_TOOLS]:
        tool = step.get("tool") if isinstance(step, Mapping) else None
        names.append(_SAFE_NAME.sub("", tool)[:40] if isinstance(tool, str) else "?")
    return tuple(names)


def validate_plan(
    raw: object, invoice_number: str, *, max_steps: int = MAX_PLAN_STEPS
) -> Plan | PlanRejection:
    """A valid ``Plan`` for ``invoice_number``, or the reasons the whole plan is rejected."""
    if not isinstance(raw, Mapping):
        return PlanRejection(("NOT_AN_OBJECT",), ())
    steps = raw.get("steps")
    tools = _tool_names(steps)
    reasons: set[str] = set()
    if set(raw) != {"invoice_number", "steps"}:
        reasons.add("UNEXPECTED_KEYS")
    if raw.get("invoice_number") != invoice_number:
        reasons.add("WRONG_INVOICE")
    if not isinstance(steps, list):
        return PlanRejection(tuple(sorted(reasons | {"STEPS_NOT_A_LIST"})), tools)
    if not steps:
        reasons.add("NO_STEPS")
    if len(steps) > min(max_steps, MAX_PLAN_STEPS):
        reasons.add("TOO_MANY_STEPS")

    built: list[PlanStep] = []
    for step in steps:
        if not isinstance(step, Mapping) or set(step) != {"tool", "args", "reason"}:
            reasons.add("INVALID_STEP")
            continue
        tool, args, reason = step["tool"], step["args"], step["reason"]
        if not isinstance(tool, str) or tool not in TOOL_REGISTRY:
            reasons.add("UNKNOWN_TOOL")
            continue
        if not isinstance(args, Mapping) or not isinstance(reason, str):
            reasons.add("INVALID_STEP")
            continue
        try:
            built.append(PlanStep(tool, dict(args), _plain(reason)))
        except ValidationError:
            reasons.add("INVALID_ARGS")
    if reasons:
        return PlanRejection(tuple(sorted(reasons)), tools)
    try:
        return Plan(invoice_number, tuple(built))
    except ValueError:
        return PlanRejection(("INCONSISTENT_PLAN",), tools)
