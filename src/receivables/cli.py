"""Command line: ``seed``, ``reset`` and ``create-user`` (SYNTHETIC DEMO DATA only).

The database URL and fixtures directory come from settings (environment), never from a
request. The system clock is used only here, at the application edge.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import date
from getpass import getpass
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from receivables.adapters.clock import SystemClock
from receivables.adapters.config import load_config
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import (
    get_user_by_username,
    insert_user,
    load_ledger_inputs,
    table_counts,
)
from receivables.api.security import hash_password
from receivables.core.aging import aging_summary
from receivables.core.history import customer_history
from receivables.core.ledger import build_ledger
from receivables.core.money import from_fils
from receivables.core.queue import build_queue
from receivables.core.scoring import score_overdue_invoices
from receivables.seed import SEED_ORDER, ResetRefused, SeedError, reset, seed
from receivables.settings import Settings

__all__ = ["main"]

BANNER = "SYNTHETIC DEMO DATA"
MIN_PASSWORD_LENGTH = 12
_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")


class _UserError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="receivables", description=f"{BANNER} tools")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("seed", "import the golden synthetic company (idempotent)"),
        ("reset", "replace the database with a fresh seeded copy (stop the app first)"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument(
            "--as-of",
            type=date.fromisoformat,
            default=None,
            help="date for the printed summary (YYYY-MM-DD; default today)",
        )
    user = commands.add_parser("create-user", help="create a login (never via the web UI, §3.3)")
    user.add_argument("username")
    user.add_argument("--role", choices=("user", "admin"), default="user")
    user.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input instead of prompting",
    )
    return parser


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass("Password: ")
        if getpass("Repeat password: ") != password:
            raise _UserError("passwords do not match")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise _UserError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _create_user(url: str, args: argparse.Namespace, clock: SystemClock) -> None:
    if not _USERNAME.fullmatch(args.username):
        raise _UserError("username must be 1-64 characters of letters, digits and . _ @ -")
    password = _read_password(args.password_stdin)
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            if get_user_by_username(conn, args.username) is not None:
                raise _UserError(f"user {args.username!r} already exists")
            insert_user(
                conn,
                clock,
                username=args.username,
                password_hash=hash_password(password),
                role=args.role,
                actor="cli",
            )
    except IntegrityError:  # a concurrent run created the same username first
        raise _UserError(f"user {args.username!r} already exists") from None
    finally:
        engine.dispose()
    print(f"created user {args.username} ({args.role})")


def _aed(fils: int) -> str:
    return f"AED {from_fils(fils):,}"


def _summary(url: str, as_of: date, config_path: str) -> list[str]:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            counts = table_counts(conn, ("customers", "invoices", "payments", "events"))
        inputs = load_ledger_inputs(engine)
    finally:
        engine.dispose()
    config = load_config(Path(config_path)).engine
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
    print(BANNER)
    if args.command == "create-user":
        try:
            _create_user(url, args, clock)
        except _UserError as err:
            print(f"create-user failed: {err}", file=sys.stderr)
            return 1
        return 0
    as_of: date = args.as_of or clock.today()
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
    except OSError as err:
        print(f"{args.command} failed: {err}", file=sys.stderr)
        return 1
    for line in _summary(url, as_of, settings.config_path):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
