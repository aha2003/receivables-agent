"""SQLAlchemy engine with the per-connection SQLite PRAGMAs of SPEC §5."""

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

__all__ = ["CONNECTION_PRAGMAS", "make_engine"]

CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in CONNECTION_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def make_engine(url: str) -> Engine:
    """Create an engine whose every new DBAPI connection gets the SPEC §5 PRAGMAs."""
    if make_url(url).get_backend_name() != "sqlite":
        raise ValueError("only SQLite database URLs are supported")
    engine = create_engine(url)
    event.listen(engine, "connect", _apply_pragmas)
    return engine
