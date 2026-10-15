"""Classifier evaluation for the T-LLM-01 smoke gate (SPEC §6.10, §14; D-034).

``evaluate`` runs ``classify_message`` (so the §7.2 retry and fallback apply) over labelled
messages and reports accuracy and macro-F1 as exact fractions. Macro-F1 averages the per-label
F1 over every label that is expected or predicted. Fifteen messages are a smoke test only
(§6.10); the gate is accuracy >= 0.80.
"""

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.classification import classify_message

__all__ = [
    "SMOKE_GATE_ACCURACY",
    "Evaluation",
    "LabelledMessage",
    "evaluate",
    "fixture_customer_names",
    "labelled_messages",
    "macro_f1",
]

SMOKE_GATE_ACCURACY = Fraction(4, 5)


@dataclass(frozen=True, slots=True)
class LabelledMessage:
    message_id: str
    body: str
    language: str | None
    expected: str


@dataclass(frozen=True, slots=True)
class Evaluation:
    results: tuple[tuple[str, str, str], ...]  # (message_id, expected, predicted)
    accuracy: Fraction
    macro_f1: Fraction

    @property
    def passes_smoke_gate(self) -> bool:
        return self.accuracy >= SMOKE_GATE_ACCURACY


def macro_f1(pairs: Iterable[tuple[str, str]]) -> Fraction:
    """Mean per-label F1 over all labels seen as expected or predicted."""
    pairs = list(pairs)
    labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    if not labels:
        return Fraction(0)
    scores: list[Fraction] = []
    for label in labels:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        fp = sum(1 for e, p in pairs if e != label and p == label)
        fn = sum(1 for e, p in pairs if e == label and p != label)
        scores.append(Fraction(2 * tp, 2 * tp + fp + fn) if tp else Fraction(0))
    return sum(scores, Fraction(0)) / len(scores)


def evaluate(client: LLMClient, messages: Iterable[LabelledMessage]) -> Evaluation:
    results: list[tuple[str, str, str]] = []
    for m in messages:
        language = m.language if m.language in {"en", "ar"} else None
        predicted = classify_message(client, m.body, language).classification.label
        results.append((m.message_id, m.expected, predicted))
    if not results:
        raise ValueError("no labelled messages")
    correct = sum(1 for _, e, p in results if e == p)
    pairs = [(e, p) for _, e, p in results]
    return Evaluation(tuple(results), Fraction(correct, len(results)), macro_f1(pairs))


def labelled_messages(fixtures: Path) -> list[LabelledMessage]:
    """The 15 synthetic §6.10 messages with their expected labels."""
    with (fixtures / "correspondence_labeled.csv").open(encoding="utf-8", newline="") as f:
        return [
            LabelledMessage(r["message_id"], r["body"], r["language"], r["expected_label"])
            for r in csv.DictReader(f)
        ]


def fixture_customer_names(fixtures: Path) -> tuple[str, ...]:
    """The synthetic customer names, for redaction while evaluating or replaying."""
    with (fixtures / "customers.csv").open(encoding="utf-8", newline="") as f:
        return tuple(r["customer_name"] for r in csv.DictReader(f))
