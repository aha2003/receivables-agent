"""The LLM as the agent's planner port (SPEC §18.6; D-037).

``make_llm_planner`` wraps the app's client (always behind redaction). An answer that is not a
valid plan JSON is asked for once more; a second invalid answer, a timeout or an API error
raises ``PlannerError`` and the orchestrator uses the rule planner (§18.6 step 7). What the
plan *says* is checked by ``validate_plan``, never here. ``last_call`` gives the orchestrator
the model role, prompt version, attempts and latency of the latest call for the run record
(§18.6 step 6); the clock is read here because ``agent/`` may not read it.
"""

import time
from collections.abc import Mapping
from typing import Any

from receivables.adapters.llm.base import PROMPT_VERSIONS, LLMClient
from receivables.adapters.llm.schemas import LLMCallFailed, LLMResponseInvalid
from receivables.agent.orchestrator import PlannerError

__all__ = ["LlmPlanner", "make_llm_planner"]


class LlmPlanner:
    prompt_version = PROMPT_VERSIONS["plan"]

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.last_call: dict[str, Any] | None = None

    @property
    def model(self) -> str | None:
        """The planner's model role (never an id), whatever the client did last (D-037)."""
        model_for = getattr(self._client, "model_for", None)
        return (
            str(model_for("plan"))
            if callable(model_for)
            else str(getattr(self._client, "model", "unknown"))
        )

    def propose(self, observation: Mapping[str, Any]) -> object:
        started = time.perf_counter()
        self.last_call = None
        attempts = 0
        outcome = "error"
        try:
            for _ in (1, 2):  # §18.6 step 7: invalid JSON twice falls back
                attempts += 1
                try:
                    plan = self._client.plan(observation).model_dump(mode="json")
                except LLMResponseInvalid:
                    outcome = "invalid_output"
                    continue
                outcome = "ok"
                return plan
            raise PlannerError("invalid plan output twice")
        except LLMCallFailed as error:
            outcome = type(error).__name__
            raise PlannerError(outcome) from None
        except PlannerError:
            raise
        except Exception as error:  # anything else is recorded by type, then falls back
            outcome = type(error).__name__
            raise
        finally:
            self.last_call = {
                "model": self.model,
                "prompt_version": self.prompt_version,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "outcome": outcome,
            }


def make_llm_planner(client: LLMClient) -> LlmPlanner:
    return LlmPlanner(client)
