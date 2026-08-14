"""Declarative base and async engine/session construction.

Kept deliberately thin: a base class and two factory functions. Connection pooling
policy, retry, and settings loading belong to whichever ticket first needs them
(T-013 onward) — this ticket only needs a metadata target for Alembic and something
tests can connect with.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for every control-plane ORM model."""


def make_engine(database_url: str) -> AsyncEngine:
    """Build an async engine for `database_url` (a `postgresql+asyncpg://...` DSN)."""
    return create_async_engine(database_url)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to `engine`."""
    return async_sessionmaker(engine, expire_on_commit=False)
