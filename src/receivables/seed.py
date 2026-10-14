"""Seed and reset the synthetic demo company (SPEC §5 purge semantics; SYNTHETIC DEMO DATA).

``seed`` imports the golden fixtures through the strict importer; running it again reports
duplicates and changes nothing.

``reset`` gives the §5 end state (fresh migrated database, golden data, one audit row) without
ever leaving a half-finished database: every precondition is checked and every fixture read
first, the replacement is migrated, seeded and audited in a staging file next to the database,
and only then are the old file and its ``-wal``/``-shm`` deleted and the staging file moved
into place. If anything fails before the swap, the old database is untouched. Reset refuses
anything that is not a regular SQLite file, and refuses while another connection is writing.
It cannot detect idle connections held by another process, so stop the app first
(docs/DECISIONS.md D-014).
"""

import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from receivables.adapters.clock import Clock
from receivables.adapters.db.controls import carry_kill_switch, runtime_kill_switch_in_file
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import ImportResult, import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import table_counts, write_audit

__all__ = ["SEED_ORDER", "ResetRefused", "ResetResult", "SeedError", "reset", "seed"]

SEED_ORDER = ("customers", "invoices", "payments", "events")
STAGING_SUFFIX = ".reset-tmp"
_SQLITE_HEADER = b"SQLite format 3\x00"


class SeedError(RuntimeError):
    """A golden fixture is missing or was rejected by the importer."""


class ResetRefused(RuntimeError):
    """The configured database is not something reset may replace right now."""


@dataclass(frozen=True, slots=True)
class ResetResult:
    path: Path
    imports: list[ImportResult]
    counts: dict[str, int]


def _read_fixtures(fixtures_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for kind in SEED_ORDER:
        path = fixtures_dir / f"{kind}.csv"
        try:
            files[kind] = path.read_bytes()
        except FileNotFoundError:
            raise SeedError(f"missing fixture {path}") from None
    return files


def _seed_files(
    engine: Engine, files: Mapping[str, bytes], clock: Clock, actor: str
) -> list[ImportResult]:
    results: list[ImportResult] = []
    for kind in SEED_ORDER:
        name = f"{kind}.csv"
        result = import_csv(engine, kind, files[kind], filename=name, clock=clock, actor=actor)
        if result.status == "rejected":
            raise SeedError(f"{name} rejected: {[(e.line, e.code) for e in result.errors]}")
        results.append(result)
    return results


def seed(
    engine: Engine, fixtures_dir: Path, clock: Clock, actor: str = "seed"
) -> list[ImportResult]:
    """Import the golden customers, invoices, payments and events, in dependency order."""
    return _seed_files(engine, _read_fixtures(fixtures_dir), clock, actor)


def _siblings(path: Path) -> tuple[Path, Path, Path]:
    return path, Path(f"{path}-wal"), Path(f"{path}-shm")


def _require_regular_or_absent(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ResetRefused(f"{path} is a symlink; refusing to replace it")
    if not stat.S_ISREG(mode):
        raise ResetRefused(f"{path} is not a regular file; refusing to replace it")


def _database_file(url: str) -> Path:
    """The SQLite file named by ``url``, after every check that guards the delete."""
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite":
        raise ResetRefused("reset only supports SQLite database URLs")
    if not database or database == ":memory:" or database.startswith("file:"):
        raise ResetRefused("reset needs a SQLite database file, not an in-memory database")
    path = Path(database)
    for candidate in (*_siblings(path), *_siblings(Path(f"{path}{STAGING_SUFFIX}"))):
        _require_regular_or_absent(candidate)
    if path.exists():
        with path.open("rb") as f:
            header = f.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            raise ResetRefused(f"{path} is not a SQLite database; refusing to delete it")
    return path


def _ensure_not_in_use(path: Path) -> None:
    """Refuse if another connection holds a write transaction on the old database."""
    probe = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.execute("ROLLBACK")
    except sqlite3.OperationalError as err:
        raise ResetRefused(f"{path} is in use by another connection ({err})") from None
    finally:
        probe.close()


def _remove(paths: tuple[Path, ...]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


def reset(url: str, fixtures_dir: Path, clock: Clock, actor: str = "reset") -> ResetResult:
    """Replace the database with a freshly migrated, seeded and audited copy (§5)."""
    path = _database_file(url)
    files = _read_fixtures(fixtures_dir)
    existed = path.exists()
    staging = Path(f"{path}{STAGING_SUFFIX}")
    staging_url = make_url(url).set(database=str(staging)).render_as_string(hide_password=False)
    _remove(_siblings(staging))  # leftovers of an interrupted earlier reset
    switch_on = existed and runtime_kill_switch_in_file(path)  # an engaged switch survives
    try:
        upgrade_to_head(staging_url)
        engine = make_engine(staging_url)
        try:
            imports = _seed_files(engine, files, clock, actor)
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
                if switch_on:
                    carry_kill_switch(conn, clock, actor=actor, reason="database.reset")
            with engine.connect() as conn:
                counts = table_counts(conn)
        finally:
            engine.dispose()  # last connection closed: SQLite checkpoints and drops the WAL
        if existed:
            _ensure_not_in_use(path)
        _remove(_siblings(path))
        os.replace(staging, path)
    finally:
        _remove(_siblings(staging))
    return ResetResult(path=path, imports=imports, counts=counts)
