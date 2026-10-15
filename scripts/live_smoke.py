"""Manual live smoke test: classify the synthetic M-01 message with Claude (SPEC §3.2; D-032).

Run only by hand, never in CI or by the test suite:

    LLM_MODE=live uv run --env-file .env python scripts/live_smoke.py

It refuses to run unless ``LLM_MODE=live`` and the API key and three model ids are set. The
message and customer names are the synthetic fixtures; the text goes through the same redaction
as the app (D-031). It prints the label, confidence, review flag, error code, attempts, latency
and token counts, and never the key, the model ids or the model's raw output.
"""

import csv
import sys
import time
from pathlib import Path

from receivables.adapters.config import load_config
from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.classification import classify_message
from receivables.adapters.llm.factory import LiveModeUnavailable, make_llm_client
from receivables.adapters.llm.mode import LlmModeConflict
from receivables.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
MESSAGE_ID = "M-01"


def _rows(name: str) -> list[dict[str, str]]:
    with (FIXTURES / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    settings = Settings()
    if settings.llm_mode != "live":
        print("refused: set LLM_MODE=live to run the live smoke test", file=sys.stderr)
        return 2
    config = load_config(ROOT / settings.config_path)
    names = tuple(row["customer_name"] for row in _rows("customers.csv"))
    try:
        client = make_llm_client(settings, config, customer_names=lambda: names)
    except (LiveModeUnavailable, LlmModeConflict) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    rows = _rows("correspondence_labeled.csv")
    message = next(row for row in rows if row["message_id"] == MESSAGE_ID)
    started = time.perf_counter()
    outcome = classify_message(client, message["body"], message["language"])
    latency_ms = round((time.perf_counter() - started) * 1000)
    inner = client.inner
    tokens = inner.usage() if isinstance(inner, RawLLMClient) else (None, None)
    input_tokens, output_tokens = tokens
    result = outcome.classification
    print(f"message:      {MESSAGE_ID} (synthetic)")
    print(f"label:        {result.label} (expected {message['expected_label']})")
    print(f"confidence:   {result.confidence}")
    print(f"needs_review: {outcome.needs_review}")
    print(f"error:        {outcome.error}")
    print(f"attempts:     {outcome.attempts}")
    print(f"latency_ms:   {latency_ms}")
    print(f"tokens:       {input_tokens} in, {output_tokens} out")
    return 0 if outcome.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
