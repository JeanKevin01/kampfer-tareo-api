"""Entorno de Alembic.

La URL de la BD se deriva de DATABASE_URL (la misma que usa la app), forzando el
driver SÍNCRONO psycopg2 para las migraciones — la aplicación sigue usando asyncpg.
No hay modelos ORM: las migraciones son SQL explícito (target_metadata = None).
"""
from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from alembic import context

config = context.config


def _sync_url() -> str:
    url = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/postgres").strip()
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    if url.startswith("postgres://"):           # estilo heroku
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):         # forzar psycopg2 (síncrono)
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


config.set_main_option("sqlalchemy.url", _sync_url())

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

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
