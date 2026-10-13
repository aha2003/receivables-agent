"""Seed and reset the synthetic demo company (SPEC §5 purge semantics; SYNTHETIC DEMO DATA).

``seed`` imports the golden fixtures through the strict importer; running it again reports
duplicates and changes nothing. ``reset`` follows §5: delete the database file and its
``-wal``/``-shm`` files, migrate a fresh file, seed it and write one audit row. It refuses
anything that is not a SQLite file database (docs/DECISIONS.md D-014).
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from receivables.adapters.clock import Clock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import ImportResult, import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import table_counts, write_audit

__all__ = ["SEED_ORDER", "ResetRefused", "ResetResult", "SeedError", "reset", "seed"]

SEED_ORDER = ("customers", "invoices", "payments", "events")
_SQLITE_HEADER = b"SQLite format 3\x00"


class SeedError(RuntimeError):
    """A golden fixture was rejected by the importer (the fixtures or importer are broken)."""


class ResetRefused(RuntimeError):
    """The configured database is not something reset may delete."""


@dataclass(frozen=True, slots=True)
class ResetResult:
    path: Path
    imports: list[ImportResult]
    counts: dict[str, int]


def seed(
    engine: Engine, fixtures_dir: Path, clock: Clock, actor: str = "seed"
) -> list[ImportResult]:
    """Import the golden customers, invoices, payments and events, in dependency order."""
    results: list[ImportResult] = []
    for kind in SEED_ORDER:
        path = fixtures_dir / f"{kind}.csv"
        result = import_csv(
            engine, kind, path.read_bytes(), filename=path.name, clock=clock, actor=actor
        )
        if result.status == "rejected":
            raise SeedError(f"{path.name} rejected: {[(e.line, e.code) for e in result.errors]}")
        results.append(result)
    return results


def _database_file(url: str) -> Path:
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite":
        raise ResetRefused("reset only supports SQLite database URLs")
    if not database or database == ":memory:" or database.startswith("file:"):
        raise ResetRefused("reset needs a SQLite database file, not an in-memory database")
    path = Path(database)
    if path.is_dir():
        raise ResetRefused(f"{path} is a directory")
    if path.exists():
        with path.open("rb") as f:
            header = f.read(len(_SQLITE_HEADER))
        if header and header != _SQLITE_HEADER:
            raise ResetRefused(f"{path} is not a SQLite database; refusing to delete it")
    return path


def reset(url: str, fixtures_dir: Path, clock: Clock, actor: str = "reset") -> ResetResult:
    """Delete the database file (and -wal/-shm), migrate a fresh one, seed it, audit it."""
    path = _database_file(url)
    existed = path.exists()
    for victim in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        victim.unlink(missing_ok=True)
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        imports = seed(engine, fixtures_dir, clock, actor=actor)
        with engine.begin() as conn:
            write_audit(
                conn,
                clock,
                actor=actor,
                action="database.reset",
                entity_type="database",
                entity_id=path.name,
                before={"existing_file": existed},
                after={
                    "row_counts": table_counts(conn),
                    "imports": [r.import_id for r in imports],
                },
            )
        with engine.connect() as conn:
            counts = table_counts(conn)
    finally:
        engine.dispose()
    return ResetResult(path=path, imports=imports, counts=counts)
