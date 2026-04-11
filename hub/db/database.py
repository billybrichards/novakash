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


def _normalize_async_dsn(url: str) -> str:
    """Ensure a DATABASE_URL uses the asyncpg SQLAlchemy dialect.

    The shared DATABASE_URL secret is set to `postgresql://...` for
    compatibility with psycopg2-based tools (migrations, ad-hoc psql,
    legacy engine services). The hub uses SQLAlchemy's
    `create_async_engine` which requires the `postgresql+asyncpg://`
    dialect prefix — without it SQLAlchemy routes to the sync psycopg2
    dialect and the hub container crashes at startup with
    `ModuleNotFoundError: No module named 'psycopg2'` (psycopg2 is not
    in hub/requirements.txt because the hub only needs asyncpg).

    This normaliser handles the common variants without being
    prescriptive: strip existing driver suffixes and canonicalise to
    `postgresql+asyncpg://`. Already-correct URLs are a no-op.

    Discovered during the DEP-02 AWS hub migration (2026-04-11): the
    HUB_HOST / HUB_SSH_KEY / JWT_SECRET secrets unblocked the deploy,
    then the container crashed on the DATABASE_URL dialect mismatch.
    """
    if not url:
        return url
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgres+asyncpg://",
        "postgres+psycopg2://",
        "postgres://",
        "postgresql://",
    ):
        if url.startswith(prefix):
            # Replace whatever we found with the canonical async form.
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


async def init_db() -> None:
    """Create the async engine and session factory."""
    global _engine, _session_factory

    settings = get_settings()
    dsn = _normalize_async_dsn(settings.DATABASE_URL)
    _engine = create_async_engine(
        dsn,
        echo=settings.DEBUG,
        pool_size=10,
        max_overflow=5,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    # Log the normalised dsn so we can see the dialect in startup logs,
    # but don't log credentials — redact everything before the `@` host.
    log.info("db.engine_created", url=_redact_dsn(dsn))


def _redact_dsn(dsn: str) -> str:
    """Return the dsn with credentials replaced by ``***`` for logging."""
    if "@" not in dsn:
        return dsn
    scheme_end = dsn.find("://")
    if scheme_end < 0:
        return dsn
    scheme = dsn[: scheme_end + 3]
    rest = dsn[scheme_end + 3:]
    at = rest.find("@")
    host = rest[at + 1:]
    return f"{scheme}***@{host}"


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
