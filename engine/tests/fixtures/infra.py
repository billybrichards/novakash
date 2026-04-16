"""Infrastructure test stubs — settings, sessions.

For `integration` tier tests, real SQLite sessions live here. For fast
tests, callers should prefer `ports.py` fakes over real DB.
"""
from __future__ import annotations

from typing import AsyncIterator

from config.settings import TestSettings


def test_settings(**overrides) -> TestSettings:
    """Return a TestSettings instance with optional field overrides."""
    return TestSettings(**overrides)


async def sqlite_session() -> AsyncIterator:
    """Create an in-memory SQLite async session for integration tests.

    Usage:
        async for session in sqlite_session():
            repo = SQLTradeRepository(session)
            ...
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
