"""Database engine construction and migration entry points."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=15000 -c lock_timeout=5000",
        },
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session, session.begin():
        yield session


def run_migrations(database_url: str) -> None:
    config = Config("alembic.ini")
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
