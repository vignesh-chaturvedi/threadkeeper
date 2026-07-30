"""Alembic environment.

Two deliberate choices:
  * The URL comes from app.settings, never from alembic.ini. One DSN, one place.
  * `target_metadata` is None. This project writes migrations by hand rather than
    autogenerating from ORM models — there are no ORM models, and hand-written
    DDL is what you want for things like `FOR UPDATE SKIP LOCKED` tables and
    pgvector indexes anyway.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.settings import get_settings

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().alembic_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
