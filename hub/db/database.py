"""
Database — SQLAlchemy Async Engine + Session Factory

Provides:
  - async engine and session maker
  - get_session() FastAPI dependency (yields AsyncSession)
  - get_settings() for lazy settings access
  - init_db() / close_db() lifecycle hooks
"""

from __future__ import annotations

from functools import lru_cache
from typing import AsyncGenerator

import structlog
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

log = structlog.get_logger(__name__)


class Settings(BaseSettings):
    """Hub configuration — loaded from environment / .env file."""

    DATABASE_URL: str = "postgresql+asyncpg://trader:trader@db:5432/trader"
    SECRET_KEY: str = "changeme-in-production-please"
    DEBUG: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ─── SQLAlchemy Setup ─────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Create the async engine and session factory."""
    global _engine, _session_factory

    settings = get_settings()
    _engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_size=10,
        max_overflow=5,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    log.info("db.engine_created", url=settings.DATABASE_URL)


async def close_db() -> None:
    """Dispose the async engine."""
    if _engine:
        await _engine.dispose()
        log.info("db.engine_disposed")


async def get_pool():
    """
    Return the raw asyncpg pool from the SQLAlchemy async engine.
    Used by endpoints that need raw SQL queries (e.g., paper trading API).
    """
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _engine.raw_connection


class _PoolProxy:
    """Provides an asyncpg-style acquire() context manager from SQLAlchemy engine."""

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    class _ConnProxy:
        def __init__(self, engine: AsyncEngine):
            self._engine = engine
            self._conn = None

        async def __aenter__(self):
            self._conn = await self._engine.raw_connection()
            # Get the underlying asyncpg connection
            return self._conn.connection.dbapi_connection

        async def __aexit__(self, *args):
            if self._conn:
                await self._conn.close()

    def acquire(self):
        return self._ConnProxy(self._engine)


_pool_proxy: _PoolProxy | None = None


async def get_asyncpg_pool() -> _PoolProxy:
    """Return a pool-like proxy for raw asyncpg access."""
    global _pool_proxy
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    if _pool_proxy is None:
        _pool_proxy = _PoolProxy(_engine)
    return _pool_proxy


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.

    Usage:
        @router.get("/endpoint")
        async def handler(session: AsyncSession = Depends(get_session)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
