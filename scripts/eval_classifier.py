"""Manual T-LLM-01 smoke gate: classify the 15 synthetic §6.10 messages live (SPEC §14; D-034).

Run only by hand, never in CI or by the test suite:

    LLM_MODE=live uv run --env-file .env python scripts/eval_classifier.py
    LLM_MODE=live uv run --env-file .env python scripts/eval_classifier.py --record

It refuses to run unless ``LLM_MODE=live`` and the key and three model ids are set, and while
``ANTHROPIC_LOG`` is set. Messages go through the app's redaction. It prints, per message, only
the expected and predicted labels, then accuracy and macro-F1 against the gate (accuracy >=
0.80). ``--record`` also writes ``tests/cassettes/t_llm_01.json``: the redacted requests and raw
responses, with the model's role (never its id), which the test suite then replays to enforce
the gate. Exit status 0 means the gate passed.
"""

import argparse
import os
import sys
from pathlib import Path

from receivables.adapters.config import load_config
from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.evaluation import (
    SMOKE_GATE_ACCURACY,
    evaluate,
    fixture_customer_names,
    labelled_messages,
)
from receivables.adapters.llm.factory import LiveModeUnavailable, make_llm_client
from receivables.adapters.llm.mode import LlmModeConflict
from receivables.adapters.llm.replay import RecordingClient
from receivables.adapters.log import configure_logging
from receivables.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CASSETTE = ROOT / "tests" / "cassettes" / "t_llm_01.json"


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(description="Manual T-LLM-01 smoke gate (live).")
    args.add_argument("--record", action="store_true", help=f"write {CASSETTE.name}")
    record = args.parse_args(argv).record
    settings = Settings()
    if settings.llm_mode != "live":
        print("refused: set LLM_MODE=live to run the live evaluation", file=sys.stderr)
        return 2
    if os.environ.get("ANTHROPIC_LOG"):
        print("refused: unset ANTHROPIC_LOG to run the live evaluation", file=sys.stderr)
        return 2
    config = load_config(ROOT / settings.config_path)
    names = fixture_customer_names(FIXTURES)
    configure_logging(lambda: names)
    try:
        client = make_llm_client(settings, config, customer_names=lambda: names)
    except (LiveModeUnavailable, LlmModeConflict) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    recorder: RecordingClient | None = None
    if record and isinstance(client.inner, RawLLMClient):
        recorder = RecordingClient(client.inner)
        client.inner = recorder
    result = evaluate(client, labelled_messages(FIXTURES))
    for message_id, expected, predicted in result.results:
        mark = "ok  " if expected == predicted else "MISS"
        print(f"{mark} {message_id}: expected {expected}, got {predicted}")
    print(f"accuracy: {float(result.accuracy):.3f} (gate {float(SMOKE_GATE_ACCURACY):.2f})")
    print(f"macro-F1: {float(result.macro_f1):.3f}")
    print(f"gate:     {'passed' if result.passes_smoke_gate else 'FAILED'}")
    if recorder is not None:
        recorder.write(CASSETTE, "t_llm_01")
        print(f"recorded: {CASSETTE.relative_to(ROOT)}")
    return 0 if result.passes_smoke_gate else 1


if __name__ == "__main__":
    sys.exit(main())
