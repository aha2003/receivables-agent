"""SQLAlchemy engine with the per-connection SQLite PRAGMAs of SPEC §5."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

__all__ = ["CONNECTION_PRAGMAS", "make_engine"]

CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def _disable_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
    # pysqlite otherwise defers BEGIN until the first write, so reads at the start of a
    # transaction run in autocommit. SQLAlchemy's documented SQLite recipe: let the "begin"
    # event emit BEGIN explicitly (D-013).
    dbapi_connection.isolation_level = None


def _emit_begin(conn: Any) -> None:
    mode = conn.get_execution_options().get("sqlite_begin", "DEFERRED")
    if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError(f"invalid sqlite_begin mode {mode!r}")
    conn.exec_driver_sql(f"BEGIN {mode}")


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in CONNECTION_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def make_engine(url: str) -> Engine:
    """Create an engine whose every new DBAPI connection gets the SPEC §5 PRAGMAs.

    Transactions start with an explicit ``BEGIN`` (``DEFERRED`` unless the connection sets the
    execution option ``sqlite_begin="IMMEDIATE"``). The parent directory of a file database is
    created if missing.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("only SQLite database URLs are supported")
    database = parsed.database
    if database and database != ":memory:" and not database.startswith("file:"):
        Path(database).parent.mkdir(parents=True, exist_ok=True)  # e.g. data/ on a fresh clone
    engine = create_engine(url)
    event.listen(engine, "connect", _disable_driver_transactions)
    event.listen(engine, "connect", _apply_pragmas)
    event.listen(engine, "begin", _emit_begin)
    return engine
