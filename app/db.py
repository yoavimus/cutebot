"""Async SQLAlchemy engine, session factory, and table bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _normalize_db_url(url: str) -> str:
    # ponytail: prefix swap; Railway injects postgresql:// / postgres://, asyncpg needs +asyncpg
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def make_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async engine. Tests pass their own in-memory URL."""
    url = _normalize_db_url(database_url or get_settings().database_url)
    return create_async_engine(url, future=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Process-wide engine / sessionmaker used by the app (routes + scheduler).
_engine: AsyncEngine = make_engine()
SessionLocal: async_sessionmaker[AsyncSession] = make_sessionmaker(_engine)


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Create tables. v1 dev convenience — use Alembic migrations for prod schema changes."""
    target = engine or _engine
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session."""
    async with SessionLocal() as session:
        yield session
