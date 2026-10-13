"""Command line: ``receivables seed`` and ``receivables reset`` (SYNTHETIC DEMO DATA only).

The database URL and fixtures directory come from settings (environment), never from a
request. The system clock is used only here, at the application edge.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from receivables.adapters.clock import SystemClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import load_ledger_inputs, table_counts
from receivables.core.aging import aging_summary
from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.ledger import build_ledger
from receivables.core.money import from_fils
from receivables.core.queue import build_queue
from receivables.core.scoring import score_overdue_invoices
from receivables.seed import SEED_ORDER, ResetRefused, SeedError, reset, seed
from receivables.settings import Settings

__all__ = ["main"]

BANNER = "SYNTHETIC DEMO DATA"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="receivables", description=f"{BANNER} tools")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("seed", "import the golden synthetic company (idempotent)"),
        ("reset", "delete the database, migrate a fresh one and seed it"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument(
            "--as-of",
            type=date.fromisoformat,
            default=None,
            help="date for the printed summary (YYYY-MM-DD; default today)",
        )
    return parser


def _aed(fils: int) -> str:
    return f"AED {from_fils(fils):,}"


def _summary(url: str, as_of: date) -> list[str]:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            counts = table_counts(conn, ("customers", "invoices", "payments", "events"))
        inputs = load_ledger_inputs(engine)
    finally:
        engine.dispose()
    config = EngineConfig()
    rows = build_ledger(inputs.invoices, inputs.payments, as_of=as_of)
    totals = aging_summary(rows)
    history = customer_history(rows, as_of, config)
    queue = build_queue(score_overdue_invoices(rows, inputs.events, history, as_of, config), config)
    return [
        " ".join(f"{name}={count}" for name, count in counts.items()),
        f"as of {as_of.isoformat()}: total open {_aed(totals.total_open)}, "
        f"overdue {_aed(totals.overdue_total)}, queue {len(queue.items)} items",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()
    clock = SystemClock()
    fixtures = Path(settings.fixtures_dir)
    url = settings.database_url
    as_of: date = args.as_of or clock.today()
    print(BANNER)
    try:
        if args.command == "reset":
            result = reset(url, fixtures, clock, actor="cli")
            print(f"reset {result.path}")
            for r in result.imports:
                print(f"  imported {r.rows_total} rows")
        else:
            upgrade_to_head(url)
            engine = make_engine(url)
            try:
                results = seed(engine, fixtures, clock, actor="cli")
            finally:
                engine.dispose()
            for kind, r in zip(SEED_ORDER, results, strict=True):
                print(f"{kind}: {r.status} ({r.rows_total} rows)")
    except ResetRefused as err:
        print(f"reset refused: {err}", file=sys.stderr)
        return 2
    except SeedError as err:
        print(f"seed failed: {err}", file=sys.stderr)
        return 1
    for line in _summary(url, as_of):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
