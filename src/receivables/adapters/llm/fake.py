"""Scripted LLM client for tests (SPEC §3.5). No network, no model.

Scripts give raw response text per call kind (a list consumed in order, or a function of the
request), so tests can also script invalid output; the text goes through the same validation
as every other client. Every request is recorded for assertions.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.schemas import KINDS

__all__ = ["FakeClient", "FakeScriptExhausted"]

Script = Iterable[str | Mapping[str, Any]] | Callable[[Mapping[str, Any]], str | Mapping[str, Any]]


class FakeScriptExhausted(LookupError):
    """The test asked for more responses of a kind than it scripted."""


def _text(value: str | Mapping[str, Any]) -> str:
    return value if isinstance(value, str) else json.dumps(value)


class FakeClient(RawLLMClient):
    model = "fake"

    def __init__(self, **scripts: Script) -> None:
        unknown = set(scripts) - set(KINDS)
        if unknown:
            raise ValueError(f"unknown call kinds: {sorted(unknown)}")
        self._functions: dict[str, Callable[[Mapping[str, Any]], str | Mapping[str, Any]]] = {}
        self._queues: dict[str, list[str]] = {}
        for kind, script in scripts.items():
            if callable(script):
                self._functions[kind] = script
            else:
                self._queues[kind] = [_text(item) for item in script]
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        self.calls.append((kind, payload))
        if kind in self._functions:
            return _text(self._functions[kind](payload))
        queue = self._queues.get(kind)
        if not queue:
            raise FakeScriptExhausted(f"no scripted {kind} response left")
        return queue.pop(0)
