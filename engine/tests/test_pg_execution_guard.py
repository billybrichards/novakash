"""Tests for PgWindowExecutionGuard."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from adapters.persistence.pg_execution_guard import PgWindowExecutionGuard


@pytest.mark.asyncio
async def test_has_not_executed_initially():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is False


@pytest.mark.asyncio
async def test_in_memory_cache_hit_after_mark():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    guard = PgWindowExecutionGuard(pool)
    await guard.mark_executed("v4_fusion", 1713000000, "order-123")
    # Second call should hit in-memory cache, NOT DB
    pool.fetchrow.reset_mock()
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    pool.fetchrow.assert_not_called()  # cache hit, no DB call


@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=Exception("DB down"))
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True  # FAIL-CLOSED: assume already executed


@pytest.mark.asyncio
async def test_load_recent_warms_cache():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"strategy_id": "v4_fusion", "window_ts": 1713000000},
        {"strategy_id": "v4_down_only", "window_ts": 1713000000},
    ])
    guard = PgWindowExecutionGuard(pool)
    await guard.load_recent(hours=2)
    pool.fetchrow = AsyncMock(return_value=None)  # returns None for cache misses
    assert await guard.has_executed("v4_fusion", 1713000000) is True
    assert await guard.has_executed("v4_down_only", 1713000000) is True
    # Cache-warmed keys don't call DB; only the unknown key does
    assert pool.fetchrow.call_count == 0  # first two were cache hits
    assert await guard.has_executed("v4_fusion", 9999999999) is False
    assert pool.fetchrow.call_count == 1  # unknown key hit DB
