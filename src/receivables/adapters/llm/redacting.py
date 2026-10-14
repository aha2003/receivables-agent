"""Redaction in front of every LLM client (SPEC §7.5, NFR-04; docs/DECISIONS.md D-031).

``RedactingClient`` wraps any ``LLMClient`` and runs ``prepare_for_llm`` on everything it is
about to send: the message text of a classify call, and every string value (at any depth) of a
plan observation or a draft fact block. With ``pseudonymize_customer_names`` on, the draft
facts therefore carry ``[CUSTOMER]`` and code puts the real name back after validation (S3.07).
Mapping keys are code-defined field names and are left as they are; numbers, booleans and
``None`` pass through unchanged.

Customer names are read through ``customer_names`` at every call, so a newly imported customer
is covered at once. ``make_llm_client`` always returns this wrapper.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.schemas import Classification, DraftResponse, PlanResponse
from receivables.core.redact import prepare_for_llm

__all__ = ["RedactingClient"]


class RedactingClient:
    def __init__(
        self,
        inner: LLMClient,
        *,
        redact_before_send: bool,
        pseudonymize_customer_names: bool,
        customer_names: Callable[[], Iterable[str]],
    ) -> None:
        self.inner = inner
        self._redact = redact_before_send
        self._pseudonymize = pseudonymize_customer_names
        self._customer_names = customer_names

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "unknown"))

    def _clean(self, value: Any, names: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return prepare_for_llm(
                value,
                redact_before_send=self._redact,
                pseudonymize_customer_names=self._pseudonymize,
                customer_names=names,
            )
        if isinstance(value, Mapping):
            return {key: self._clean(item, names) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._clean(item, names) for item in value]
        return value

    def _names(self) -> tuple[str, ...]:
        return tuple(self._customer_names()) if self._pseudonymize else ()

    def classify(self, text: str, language_hint: str | None) -> Classification:
        return self.inner.classify(self._clean(text, self._names()), language_hint)

    def plan(self, observation: Mapping[str, Any]) -> PlanResponse:
        return self.inner.plan(self._clean(observation, self._names()))

    def draft(self, facts: Mapping[str, Any]) -> DraftResponse:
        return self.inner.draft(self._clean(facts, self._names()))
