"""
hub/db/helpers.py

Thin SQLAlchemy async query helpers shared across API modules.

These replace per-module copies of _fetch / _fetchrow / _execute / _fetchval
that were previously duplicated in trading_config.py, paper.py, etc.

Usage:
    from db.helpers import fetch, fetchrow, execute, fetchval
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def fetch(session: AsyncSession, query: str, params: dict | None = None) -> list[dict]:
    """Execute *query* and return all rows as list[dict]."""
    result = await session.execute(text(query), params or {})
    return [dict(r) for r in result.mappings().all()]


async def fetchrow(session: AsyncSession, query: str, params: dict | None = None) -> dict | None:
    """Execute *query* and return the first row as dict, or None."""
    result = await session.execute(text(query), params or {})
    row = result.mappings().first()
    return dict(row) if row else None


async def execute(session: AsyncSession, query: str, params: dict | None = None) -> None:
    """Execute *query* (INSERT/UPDATE/DELETE) and commit."""
    await session.execute(text(query), params or {})
    await session.commit()


async def fetchval(session: AsyncSession, query: str, params: dict | None = None):
    """Execute *query* and return the scalar from the first column of the first row."""
    result = await session.execute(text(query), params or {})
    row = result.first()
    return row[0] if row else None
