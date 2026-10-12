"""Programmatic Alembic upgrade and downgrade for a given database URL."""

from pathlib import Path

from alembic import command
from alembic.config import Config

__all__ = ["MIGRATIONS_DIR", "alembic_config", "downgrade_to_base", "upgrade_to_head"]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def downgrade_to_base(url: str) -> None:
    command.downgrade(alembic_config(url), "base")
