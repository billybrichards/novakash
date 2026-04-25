"""Tests for engine.adapters.prediction.timesfm_seeder.seed_timesfm_buffer.

All I/O is mocked — no real DB or HTTP calls.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from adapters.prediction.timesfm_seeder import seed_timesfm_buffer


# ── Helpers ──────────────────────────────────────────────────────────


def _make_db_pool(rows: list[dict] | None = None):
    """Return a mock asyncpg pool that yields rows from fetch()."""
    if rows is None:
        rows = []
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _make_rows(n: int) -> list[dict]:
    """Return n fake DB rows with avg_price."""
    return [{"avg_price": 84000.0 + i} for i in range(n)]


def _mock_response(status: int, json_body: dict | None = None):
    """Return a mock aiohttp response context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {})
    return resp


# ── Tests ────────────────────────────────────────────────────────────


async def test_successful_seed():
    """DB returns rows, HTTP 200 → returns len(prices)."""
    rows = _make_rows(100)
    pool, conn = _make_db_pool(rows)

    mock_resp = _mock_response(200, {"seeded": 100, "buffer_size": 900})
    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=AsyncMock())
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession") as MockCS:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        MockCS.return_value = ctx

        result = await seed_timesfm_buffer(pool, "http://ml-box:8080")

    assert result == 100


async def test_empty_db_returns_zero():
    """No ticks in DB → returns 0, no HTTP call."""
    pool, conn = _make_db_pool([])

    with patch("aiohttp.ClientSession") as MockCS:
        result = await seed_timesfm_buffer(pool, "http://ml-box:8080")

    # No HTTP session should have been created
    MockCS.assert_not_called()
    assert result == 0


async def test_404_returns_zero_no_crash():
    """ML box returns 404 (endpoint not deployed) → returns 0, no exception."""
    rows = _make_rows(50)
    pool, conn = _make_db_pool(rows)

    mock_resp = _mock_response(404)
    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=AsyncMock())
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession") as MockCS:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        MockCS.return_value = ctx

        result = await seed_timesfm_buffer(pool, "http://ml-box:8080")

    assert result == 0


async def test_500_returns_zero():
    """ML box returns 500 → returns 0."""
    rows = _make_rows(50)
    pool, conn = _make_db_pool(rows)

    mock_resp = _mock_response(500)
    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=AsyncMock())
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession") as MockCS:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        MockCS.return_value = ctx

        result = await seed_timesfm_buffer(pool, "http://ml-box:8080")

    assert result == 0


async def test_none_db_pool_returns_zero():
    """Pass None as db_pool → returns 0 (AttributeError caught)."""
    result = await seed_timesfm_buffer(None, "http://ml-box:8080")
    assert result == 0


async def test_connection_error_returns_zero():
    """aiohttp.ClientError during POST → returns 0."""
    import aiohttp

    rows = _make_rows(50)
    pool, conn = _make_db_pool(rows)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(
        side_effect=aiohttp.ClientError("connection refused")
    )

    with patch("aiohttp.ClientSession") as MockCS:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        MockCS.return_value = ctx

        result = await seed_timesfm_buffer(pool, "http://ml-box:8080")

    assert result == 0


async def test_query_uses_lookback_and_asset_params():
    """Verify the SQL query receives lookback_minutes and asset."""
    pool, conn = _make_db_pool([])

    await seed_timesfm_buffer(
        pool, "http://ml-box:8080", lookback_minutes=30, asset="ETH"
    )

    conn.fetch.assert_awaited_once()
    args = conn.fetch.call_args
    # positional args after the SQL string
    assert args[0][1] == 30  # lookback_minutes
    assert args[0][2] == "ETH"  # asset


async def test_db_query_failure_returns_zero():
    """Exception during DB query → returns 0."""
    pool = MagicMock()
    pool.acquire = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("connection lost"))
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await seed_timesfm_buffer(pool, "http://ml-box:8080")
    assert result == 0
