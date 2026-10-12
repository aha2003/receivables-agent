"""Alembic environment: migrations run through ``make_engine`` so the SPEC §5 PRAGMAs apply."""

from alembic import context

from receivables.adapters.db.engine import make_engine
from receivables.settings import Settings

config = context.config
url = config.get_main_option("sqlalchemy.url") or Settings().database_url

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
