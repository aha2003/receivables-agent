"""Manual live demo run, recorded for replay: classifications, plans and drafts (D-037).

Run only by hand, never in CI or by the test suite:

    LLM_MODE=live uv run --env-file .env python scripts/record_demo.py           # run, report
    LLM_MODE=live uv run --env-file .env python scripts/record_demo.py --record  # and record

It refuses to run unless ``LLM_MODE=live`` and the key and three model ids are set, and while
``ANTHROPIC_LOG`` is set. Everything is synthetic and goes through the app's redaction:

1. the 15 §6.10 messages are classified (the T-LLM-01 gate, accuracy >= 0.80);
2. on a throwaway database in a temporary folder (never ``data/``), seeded with the golden
   fixtures, one agent run at the configured level as of 2026-10-31 plans every case with the
   LLM planner and drafts with the LLM drafter.

It prints labels, plan outcomes and draft generators only, never text. ``--record`` writes
``tests/cassettes/live_demo.json`` (redacted requests and raw responses, the model's role and
never its id), replacing the previous recording. Replay mode then serves the classifications
and the first agent run after a reset, as of 2026-10-31, before correspondence is uploaded.
Exit status 0 means the gate passed.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.evaluation import (
    SMOKE_GATE_ACCURACY,
    evaluate,
    fixture_customer_names,
    labelled_messages,
)
from receivables.adapters.llm.factory import LiveModeUnavailable, make_llm_client
from receivables.adapters.llm.mode import LlmModeConflict
from receivables.adapters.llm.planner import make_llm_planner
from receivables.adapters.llm.replay import RecordingClient
from receivables.adapters.log import configure_logging
from receivables.seed import seed
from receivables.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CASSETTE = ROOT / "tests" / "cassettes" / "live_demo.json"
AS_OF = date(2026, 10, 31)
CLOCK = FixedClock(datetime(2026, 10, 31, 10, 0, tzinfo=DUBAI))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual live demo run (records with --record).")
    parser.add_argument("--record", action="store_true", help=f"write {CASSETTE.name}")
    record = parser.parse_args(argv).record
    settings = Settings()
    if settings.llm_mode != "live":
        print("refused: set LLM_MODE=live to run the live demo", file=sys.stderr)
        return 2
    if os.environ.get("ANTHROPIC_LOG"):
        print("refused: unset ANTHROPIC_LOG to run the live demo", file=sys.stderr)
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

    # 1. Classifications: the T-LLM-01 gate.
    result = evaluate(client, labelled_messages(FIXTURES))
    for message_id, expected, predicted in result.results:
        mark = "ok  " if expected == predicted else "MISS"
        print(f"{mark} {message_id}: expected {expected}, got {predicted}")
    print(f"accuracy: {float(result.accuracy):.3f} (gate {float(SMOKE_GATE_ACCURACY):.2f})")
    print(f"macro-F1: {float(result.macro_f1):.3f}")

    # 2. One agent run on a throwaway copy of the golden data.
    with tempfile.TemporaryDirectory(prefix="receivables-demo-") as folder:
        url = f"sqlite:///{Path(folder) / 'demo.db'}"
        upgrade_to_head(url)
        engine = make_engine(url)
        try:
            seed(engine, FIXTURES, CLOCK)
            summary = run_agent_tick(
                engine,
                CLOCK,
                config,
                as_of=AS_OF,
                planner=make_llm_planner(client),
                drafter=make_draft_writer(client),
            )
            with engine.connect() as conn:
                plans = Counter(
                    json.loads(row)["plan"]
                    for row in conn.execute(
                        text("SELECT after_json FROM audit_log WHERE action='agent.plan_received'")
                    ).scalars()
                )
                drafts = Counter(conn.execute(text("SELECT generator FROM drafts")).scalars())
        finally:
            engine.dispose()
    print(f"agent run: {summary.cases_considered} cases, fallback {summary.fallback}")
    print(f"plans:     {dict(sorted(plans.items()))} (used, rejected or failed)")
    print(f"drafts:    {dict(sorted(drafts.items()))} (llm or template)")
    print(f"gate:      {'passed' if result.passes_smoke_gate else 'FAILED'}")
    if recorder is not None:
        recorder.write(CASSETTE, "live_demo")
        shown = CASSETTE.relative_to(ROOT) if CASSETTE.is_relative_to(ROOT) else CASSETTE
        print(f"recorded:  {shown}")
    return 0 if result.passes_smoke_gate else 1


if __name__ == "__main__":
    sys.exit(main())
