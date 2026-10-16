"""The agent's LLM ports for a run (SPEC §8.2, §18.6; D-036, D-037).

In the default ``fake`` mode the app has no model: the agent plans with the rule planner and
drafts from the templates, exactly as before S3.07 (FakeClient is a test tool). In ``replay``
and ``live`` modes the planner and the drafter use the app's client, always behind redaction.
"""

from typing import Any

from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.planner import make_llm_planner

__all__ = ["llm_ports"]


def llm_ports(state: Any) -> dict[str, Any]:
    if state.llm_mode == "fake":
        return {"planner": None, "drafter": None}
    return {"planner": make_llm_planner(state.llm), "drafter": make_draft_writer(state.llm)}
