"""The connection to Postgres, and the unit of work on top of it.

One engine per process, built on first use rather than at import -- importing a
model must not require a database to exist, or the schema tests could not run
and Alembic could not read the metadata it is about to migrate.

The rule this module exists to make easy to follow:

    **Never hold a transaction across a call to a platform.**

The publisher claims a row, commits, calls Graph, then opens a second
transaction to record what happened. Wrapping all three in one `transaction()`
would keep a Postgres transaction (and a pooled connection) open for the length
of an HTTP round trip to Meta -- up to the 90 second upload timeout in
`app.platforms.facebook` -- while holding the row lock that stops a second
worker touching it. That is how a slow upload becomes a stalled queue.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    """The process's engine, built on first call."""
    global _engine
    if _engine is None:
        config = settings()
        _engine = create_async_engine(
            config.database_url,
            echo=config.db_echo,
            pool_size=config.db_pool_size,
            max_overflow=config.db_max_overflow,
            # The publisher spends most of its life idle between polls, and a
            # connection that a firewall or a Postgres restart dropped in the
            # meantime otherwise surfaces as a failed publish rather than as a
            # reconnect. One round trip on checkout is a cheap trade.
            pool_pre_ping=True,
        )
        # Redacted: this line goes into every operator's terminal, and the DSN
        # carries a password (C-5).
        logger.info("Database engine created for %s", config.redacted_database_url)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """The sessionmaker, built on first call.

    `expire_on_commit=False` is not a preference. SQLAlchemy's default expires
    every attribute at commit, so the next attribute read re-fetches -- and on
    an AsyncSession a lazy re-fetch outside an await raises MissingGreenlet
    instead of loading. Since the publisher reads a claimed row's fields after
    committing the claim, the default would turn ordinary code into an error
    that only appears once there is a real database behind it.
    """
    global _sessions
    if _sessions is None:
        _sessions = async_sessionmaker(
            engine(), expire_on_commit=False, autoflush=False
        )
    return _sessions


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One unit of work: commits on success, rolls back on anything raised.

    Short by construction. See the module docstring -- if a platform call
    belongs anywhere near this block, it belongs outside it.
    """
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            # BaseException, not Exception: a cancelled task (the publisher
            # being shut down mid-claim) must roll back too, or the row stays
            # locked until the connection is reclaimed.
            await session.rollback()
            raise


async def dispose() -> None:
    """Close every pooled connection. Call on shutdown.

    Resets the cached engine as well, so a process that disposes and carries on
    -- a test, or a worker reconnecting after a configuration change -- builds a
    fresh one rather than handing out connections from a closed pool.
    """
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _sessions = None
