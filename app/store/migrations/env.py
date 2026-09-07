"""Alembic's entry point.

Reads the DSN from `app.config` rather than from alembic.ini, so the database is
named in exactly one place and no password is ever committed (C-5).

Async, because `DATABASE_URL` carries the asyncpg driver the app itself uses.
Pointing Alembic at a second, synchronous driver would mean a second URL to keep
in step and a second set of connection semantics to be surprised by.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

# Every model must be imported before `Base.metadata` is read, or autogenerate
# will cheerfully write a migration that drops the tables it cannot see.
from app.store.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    """Options shared by the offline and online paths.

    Shared so `alembic upgrade head --sql` renders the same DDL that
    `alembic upgrade head` executes -- a divergence there means reviewing one
    thing and running another.
    """
    context.configure(
        target_metadata=target_metadata,
        # Off by default; a column's type changing is worth writing by hand.
        compare_type=True,
        # Catches a server_default added to a model but never migrated.
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Render DDL to stdout without connecting. `alembic upgrade head --sql`.

    The only way to see what a migration will do to a database you cannot reach
    from here -- which includes reviewing this one before it is ever run.
    """
    _configure(
        url=settings().database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(
        settings().database_url,
        # NullPool: a migration is one connection used once, and a pool left
        # holding connections keeps the process alive after the work is done.
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
