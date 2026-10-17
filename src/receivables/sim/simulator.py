"""The simulator's own database (SPEC §19.2; T-SIM-04; D-047).

``create_sim_db`` writes the generated company to ``<data_dir>/sim_<seed>.db`` through the
strict importer, every row stamped by a simulated clock at the run start (day 0, the tick time
in Asia/Dubai), never the wall clock. The file is built next to its destination and moved into
place only when complete. It refuses any destination that is, or shares a file with, the real
database, a symlink, or an existing file that is not SQLite, so the real database is never
opened, let alone written. The arms, the behaviour engine and the day loop come in S4.02–S4.03.
"""

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import ImportResult, import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import table_counts, write_audit
from receivables.sim.company import SimCompany, company_csv, generate_company
from receivables.sim.scenario import Scenario

__all__ = ["LABEL", "SimDbResult", "SimRefused", "create_sim_db", "sim_db_path"]

LABEL = "SIMULATION — MECHANICS, NOT EVIDENCE"
ACTOR = "simulator"
STAGING_SUFFIX = ".sim-tmp"
_SQLITE_HEADER = b"SQLite format 3\x00"


class SimRefused(RuntimeError):
    """The simulation database cannot be written where it was asked to go."""


@dataclass(frozen=True, slots=True)
class SimDbResult:
    path: Path
    company: SimCompany
    imports: list[ImportResult]
    counts: dict[str, int]


def sim_db_path(data_dir: Path, seed: int) -> Path:
    return data_dir / f"sim_{seed}.db"


def _siblings(path: Path) -> tuple[Path, ...]:
    return path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")


def _real_files(real_database_url: str) -> set[Path]:
    """The real database's file and its companions, resolved; empty when it has no file."""
    database = make_url(real_database_url).database
    if not database or database == ":memory:" or database.startswith("file:"):
        return set()
    return {p.resolve() for p in _siblings(Path(database))}


def _check_destination(path: Path, staging: Path, real_database_url: str) -> None:
    real = _real_files(real_database_url)
    for candidate in (*_siblings(path), *_siblings(staging)):
        if candidate.resolve() in real:
            raise SimRefused(f"{path} is the real database or shares its files; refusing")
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SimRefused(f"{candidate} is a symlink; refusing to replace it")
        if not stat.S_ISREG(mode):
            raise SimRefused(f"{candidate} is not a regular file; refusing to replace it")
    if path.exists():
        with path.open("rb") as f:
            if f.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                raise SimRefused(f"{path} is not a SQLite database; refusing to replace it")


def _remove(paths: tuple[Path, ...]) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


def create_sim_db(scenario: Scenario, data_dir: Path, real_database_url: str) -> SimDbResult:
    """Generate the seeded company and write it to a fresh ``sim_<seed>.db``."""
    path = sim_db_path(data_dir, scenario.seed)
    staging = Path(f"{path}{STAGING_SUFFIX}")
    _check_destination(path, staging, real_database_url)
    company = generate_company(scenario)
    files = company_csv(company)
    clock = SimulatedClock(datetime.combine(scenario.start_date, scenario.tick_time, DUBAI))
    data_dir.mkdir(parents=True, exist_ok=True)
    staging_url = f"sqlite:///{staging}"
    _remove(_siblings(staging))  # leftovers of an interrupted earlier run
    try:
        upgrade_to_head(staging_url)
        engine = make_engine(staging_url)
        try:
            imports: list[ImportResult] = []
            for kind, raw in files.items():
                result = import_csv(
                    engine, kind, raw, filename=f"sim_{kind}.csv", clock=clock, actor=ACTOR
                )
                if result.status != "accepted":
                    codes = [(e.line, e.code) for e in result.errors]
                    raise SimRefused(f"generated {kind} were not accepted: {codes}")
                imports.append(result)
            with engine.begin() as conn:
                write_audit(
                    conn,
                    clock,
                    actor=ACTOR,
                    action="sim.created",
                    entity_type="database",
                    entity_id=path.name,
                    before=None,
                    after={
                        "scenario": scenario.name,
                        "seed": scenario.seed,
                        "company_sha256": company.sha256(),
                        "imports": [r.import_id for r in imports],
                        "label": LABEL,
                    },
                )
            with engine.connect() as conn:
                counts = table_counts(conn)
        finally:
            engine.dispose()  # last connection closed: SQLite checkpoints and drops the WAL
        _remove(_siblings(path))
        os.replace(staging, path)
    finally:
        _remove(_siblings(staging))
    return SimDbResult(path=path, company=company, imports=imports, counts=counts)
